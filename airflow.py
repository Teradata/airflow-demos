#!/usr/bin/env python3
"""
generate_connection.py (dual-mode: with or without Airbyte validation)

Produces a connection bundle using RAG + Bedrock LLM, and optionally
applies the bundle to an Airbyte instance.

"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import textwrap
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
# LangChain Bedrock bindings (assumed installed in the environment)
from langchain_aws import BedrockEmbeddings, ChatBedrock
from langchain_chroma import Chroma

from llm import get_embedding_and_chroma, build_prompt, get_chat_llm
from util import _safe_filename, extract_json_from_text, find_placeholders_in_bundle, contains_placeholders

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("airflow")

# -----------------------
# Load environment
# -----------------------
load_dotenv()

AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
BEDROCK_EMBED_MODEL = os.getenv("BEDROCK_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
BEDROCK_LLM_ARN = os.getenv("BEDROCK_LLM_ARN")  # required
AIRBYTE_API_URL = os.getenv("AIRBYTE_API_URL", "").strip()
AIRBYTE_API_TOKEN = os.getenv("AIRBYTE_API_TOKEN", "").strip()
CREATE_IN_AIRBYTE_ENV = os.getenv("CREATE_IN_AIRBYTE", "false")
MAX_REPAIR_ATTEMPTS = int(os.getenv("MAX_REPAIR_ATTEMPTS", "3"))
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "6"))
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_store/current")
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION", "airbyte_connectors")
WORKSPACE_ID = os.getenv("AIRBYTE_WORKSPACE_ID", "<<WORKSPACE_ID>>")
RUN_SYNC = os.getenv("RUN_SYNC", "")

# Configurable environment variables
# Auto-detect Breeze host DAG directory
DEFAULT_BREEZE_DAGS = "/home/devtools/devtools/airflow/files/dags"

AIRFLOW_DAGS_DIR = os.getenv("AIRFLOW_DAGS_DIR", DEFAULT_BREEZE_DAGS)
DBT_DIR = os.getenv("DBT_PROJECT_DIR", "/home/devtools/devtools/airflow/files/dbt")

SAVE_DAG_DIR = os.getenv("SAVE_DAG_DIR", "./generated_dags")  # fallback local storage
AIRFLOW_SERVER = os.getenv("AIRFLOW_SERVER")
AIRFLOW_API = os.getenv("AIRFLOW_API")                    # e.g. "http://airflow:8080/api/v2"
AIRFLOW_USER = os.getenv("AIRFLOW_USER")
AIRFLOW_PASS = os.getenv("AIRFLOW_PASS")
AIRFLOW_TEMPLATE_DAG_ID = os.getenv("AIRFLOW_TEMPLATE_DAG_ID")  # a pre-deployed template DAG id
AIRFLOW_DEFAULT_OWNER = os.getenv("AIRFLOW_DEFAULT_OWNER", "airbyte_agent")







# High-level: create DAG file and deploy or save, or trigger template dag

# generate a per-connection DAG file content (agent will write this file)
def _render_dag_content(dag_id: str, connection_id: str, connection_name: str, cron_expr: str, owner: str = AIRFLOW_DEFAULT_OWNER) -> str:
    """
    Render an Airflow DAG that:
      1. Runs Airbyte sync
      2. Runs dbt run
      3. Runs dbt test
    """
    content = f'''
from datetime import datetime, timedelta
from airflow import DAG
from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.airbyte.operators.airbyte import AirbyteTriggerSyncOperator
from airflow.providers.airbyte.sensors.airbyte import AirbyteJobSensor
import os

AIRBYTE_AIRFLOW_CONN_ID = os.getenv("AIRBYTE_AIRFLOW_CONN_ID", "airbyte_default")
CONNECTION_ID = "{connection_id}"

default_args = {{
    "owner": "{owner}",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
}}

with DAG(
    dag_id="{dag_id}",
    default_args=default_args,
    schedule="{cron_expr}",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["airbyte","dbt","auto-generated"],
    is_paused_upon_creation=False,
):

    airbyte_sync = AirbyteTriggerSyncOperator(
        task_id="airbyte_sync_async",
        connection_id=CONNECTION_ID,
        airbyte_conn_id=AIRBYTE_AIRFLOW_CONN_ID,
        asynchronous=True,
    )

    wait_for_sync = AirbyteJobSensor(
        task_id="wait_for_sync",
        airbyte_job_id=airbyte_sync.output,
        poke_interval=20,
        timeout=3600,
    )



    airbyte_sync >> wait_for_sync
'''
    return textwrap.dedent(content)



# write DAG file (atomic)
def write_dag_file(dag_content: str, dag_filename: str, dag_dir: str) -> str:
    os.makedirs(dag_dir, exist_ok=True)
    dag_path = os.path.join(dag_dir, dag_filename)
    tmp_path = dag_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(dag_content)
    os.replace(tmp_path, dag_path)
    return dag_path


def airflow_get_jwt_token(api_base: str, username: str, password: str) -> str:
    url = api_base.rstrip("/") + "/auth/token"
    payload = {"username": username, "password": password}

    r = requests.post(url, json=payload, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data["access_token"]

def airflow_trigger_dag(dag_id: str, token: str, conf: dict, api_base: str):
    url = api_base.rstrip("/") + f"/dags/{dag_id}/dagRuns"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    run_id = f"airbyte__{dag_id}__{int(time.time())}"

    # Generate a valid UTC logical_date (ISO-8601)
    logical_date = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    log.debug("logical_date -- " + logical_date)

    payload = {
        "dag_run_id": run_id,
        "logical_date": logical_date,   # <-- required
        "conf": conf or {}
    }
    log.debug("trigger flow url : " + url)
    r = requests.post(url, headers=headers, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()

# Trigger Airflow template DAG via REST API (Airflow 2.x)
def trigger_airflow_template_dag_run(dag_id: str, connection_id: str, run_id: Optional[str] = None, conf: Optional[dict] = None, api_base: Optional[str] = None) -> Dict[str, Any]:
    api_base = api_base or AIRFLOW_API

    if not api_base:
        raise ValueError("AIRFLOW_API not configured for REST trigger")
    if not AIRFLOW_USER or not AIRFLOW_PASS:
        raise ValueError("AIRFLOW_USER/AIRFLOW_PASS required to call Airflow REST API")
    # Authenticate with Airflow to get JWT token
    token = airflow_get_jwt_token(AIRFLOW_SERVER, AIRFLOW_USER, AIRFLOW_PASS)
    resp = airflow_trigger_dag(
        dag_id=dag_id,
        token=token,
        conf={},
        api_base=AIRFLOW_API,
    )
    return {
        "ok": True,
        "mode": "trigger_new_dag",
        "response": resp,
        "note": f"Triggered new DAG {dag_id} via Airflow API"
    }

import re
import hashlib

def _safe_dag_id(raw: str, prefix: str = "airbyte_sync__") -> str:
    """
    Normalize, shorten, and hash DAG IDs so they always load correctly in Airflow.
    """
    # Normalize unsafe chars
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", raw)

    # Trim to ensure prefix + name < 60 chars
    max_name_len = 40  # keep filename short
    trimmed = cleaned[:max_name_len]

    # Add 6-char hash for uniqueness
    digest = hashlib.md5(raw.encode()).hexdigest()[:6]

    dag_id = f"{prefix}{trimmed}_{digest}"
    return dag_id


def create_and_deploy_airflow_dag(
        connection_bundle: dict,
        created_resp: Optional[dict] = None,
        dag_owner: str = AIRFLOW_DEFAULT_OWNER,
        prefer_dags_dir: Optional[str] = None,
        prefer_save_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Try to create+deploy an Airflow DAG for the Airbyte connection defined in connection_bundle.
    Return a dict summarizing what was done (keys: mode, path_or_resp, note).
    Modes:
      - 'write_dags_dir' -> wrote file to AIRFLOW_DAGS_DIR
      - 'trigger_template' -> triggered a pre-existing template DAG via Airflow REST
      - 'save_local' -> saved DAG file to SAVE_DAG_DIR for manual deploy
    """
    # extract schedule and connection id
    schedule = connection_bundle.get("sync", {}).get("schedule") or {}
    cron_expr = schedule.get("cronExpression") or schedule.get("cron") or None
    if not cron_expr:
        return {"ok": False, "mode": "none", "note": "No cron schedule found in bundle.sync.schedule"}

    # determine connection id (prefer Airbyte create response)
    conn_id = None
    if created_resp and isinstance(created_resp, dict):
        # attempt to find connection id in created_resp
        for k in ("connectionId", "id", "connection_id"):
            if created_resp.get(k):
                conn_id = str(created_resp.get(k))
                break
        if not conn_id:
            # permissive scan for UUID-like
            _UUID_RE = re.compile(r"[0-9a-fA-F\-]{8,36}")
            def _scan(obj):
                if isinstance(obj, str):
                    m = _UUID_RE.search(obj)
                    if m: return m.group(0)
                if isinstance(obj, dict):
                    for v in obj.values():
                        r = _scan(v)
                        if r: return r
                if isinstance(obj, list):
                    for item in obj:
                        r = _scan(item)
                        if r: return r
                return None
            conn_id = _scan(created_resp)
    if not conn_id:
        # fallback to metadata in bundle (optional)
        conn_id = connection_bundle.get("metadata", {}).get("airbyte_connection_id") or connection_bundle.get \
            ("connection_id")
    if not conn_id:
        return {"ok": False, "mode": "none", "note": "Could not determine Airbyte connection id"}

    conn_name = connection_bundle.get("connection_name", f"airbyte_conn_{conn_id}")
    dag_id = _safe_dag_id(f"{conn_name}_{conn_id}")
    log.debug("dag_id: " + dag_id)
    dag_filename = f"{dag_id}.py"
    log.debug("dag_filename : %s ", dag_filename)
    dag_content = _render_dag_content( dag_id, conn_id, conn_name, cron_expr, owner=dag_owner)

    # 1) If AIRFLOW_DAGS_DIR (writable) exists or prefer_dags_dir provided -> write DAG there
    target_dags_dir = prefer_dags_dir or AIRFLOW_DAGS_DIR
    log.debug("target_dags_dir : %s ", target_dags_dir)
    if target_dags_dir:
        try:
            dag_path = write_dag_file(dag_content, dag_filename, target_dags_dir)
            log.debug("dag_path : %s ", dag_path)
        except Exception as exc:
            log.warning("Failed to write DAG into AIRFLOW_DAGS_DIR (%s): %s", target_dags_dir, exc)

    # 2) If Airflow REST + template DAG provided -> trigger template dag run (no DAG file needed)
    if AIRFLOW_API and AIRFLOW_USER and AIRFLOW_PASS:
        log.debug("triggering dag in airflow")
        try:
            # Authenticate with Airflow to get JWT token
            token = airflow_get_jwt_token(AIRFLOW_SERVER, AIRFLOW_USER, AIRFLOW_PASS)
            wait_for_breeze_dag_load(dag_id, AIRFLOW_API, token)
            resp = trigger_airflow_template_dag_run(dag_id, conn_id, conf={"connection_id": conn_id, "airbyte_connection_name": conn_name}, api_base=AIRFLOW_API)
            log.debug("triggerd dag in airflow")
            return {"ok": True, "mode": "trigger_template", "response": resp, "note": f"Triggered template DAG {AIRFLOW_TEMPLATE_DAG_ID} via Airflow API"}
        except Exception as exc:
            log.warning("Failed to trigger newly created DAG via REST API: %s", exc)
    # 3) fallback: save to SAVE_DAG_DIR for manual deployment
    save_dir = prefer_save_dir or SAVE_DAG_DIR
    try:
        dag_path = write_dag_file(dag_content, dag_filename, save_dir)
        return {"ok": True, "mode": "save_local", "path": dag_path, "note": "Saved DAG locally for manual deployment"}
    except Exception as exc:
        log.exception("Failed to save DAG locally: %s", exc)
        return {"ok": False, "mode": "none", "note": f"Failed to save DAG: {exc}"}

import time
import requests



def wait_for_breeze_dag_load(dag_id: str, api_base: str, token: str, timeout: int = 480):
    """
    Breeze-compatible DAG loading check.
    Airflow inside Breeze re-parses DAGs every 2–10 seconds.
    """
    url = api_base.rstrip("/") + f"/dags/{dag_id}"
    headers = {"Authorization": f"Bearer {token}"}

    log.debug(f"⏳ Waiting for Airflow Breeze to detect DAG: {dag_id}")

    start = time.time()
    while time.time() - start < timeout:
        resp = requests.get(url, headers=headers)

        if resp.status_code == 200:
            log.info(f"✅ DAG loaded: {dag_id}")
            return True

        log.info("   ...DAG not yet loaded. Retrying...")
        time.sleep(3)

    raise TimeoutError(f"DAG load failed to airflow '{dag_id}' within {timeout} seconds")