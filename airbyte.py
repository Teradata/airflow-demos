
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
log = logging.getLogger("airbyte")

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



# -----------------------
# Connection id extraction
# -----------------------
_UUID_RE = re.compile(r"[0-9a-fA-F\-]{36}")


# -----------------------
# Airbyte HTTP helpers
# -----------------------
def _build_url(path: str) -> str:
    base = AIRBYTE_API_URL.rstrip("/") if AIRBYTE_API_URL else ""
    if path.startswith("/"):
        return base + path
    return base + "/" + path


def airbyte_post(path: str, body: Dict[str, Any], timeout: int = 30) -> requests.Response:
    """POST wrapper for Airbyte endpoints."""
    url = _build_url(path)
    headers = {"Content-Type": "application/json"}
    if AIRBYTE_API_TOKEN:
        headers["Authorization"] = f"Bearer {AIRBYTE_API_TOKEN}"
    log.debug("airbyte post request - %s %s", url, body)
    return requests.post(url, json=body, headers=headers, timeout=timeout)


def airbyte_get(path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> requests.Response:
    """GET wrapper for Airbyte endpoints."""
    url = _build_url(path)
    headers = {"Accept": "application/json"}
    if AIRBYTE_API_TOKEN:
        headers["Authorization"] = f"Bearer {AIRBYTE_API_TOKEN}"
    log.debug("airbyte get request - %s params=%s", url, params)
    return requests.get(url, params=params, headers=headers, timeout=timeout)


def is_airbyte_reachable(api_url: str, timeout: float = 5.0) -> bool:
    """Quick health check for Airbyte server (multiple fallbacks)."""
    if not api_url:
        return False
    health_url = api_url.rstrip("/") + "/health"
    try:
        r = requests.get(health_url, timeout=timeout)
        if r.status_code == 200:
            return True
    except Exception:
        pass

    try:
        r = requests.get(api_url.rstrip("/") + "/sources/list", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def apply_bundle_to_airbyte(
    bundle: Dict[str, Any],
    run_sync: bool = False,
    force_apply: bool = False,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Apply an existing connection_bundle JSON to Airbyte using helper functions.
    Returns (ok, response_summary). If placeholders exist and force_apply is False, returns skipped status.
    """
    placeholders = find_placeholders_in_bundle(bundle)
    if placeholders and not force_apply:
        msg = {
            "status": "skipped",
            "reason": "placeholders_present",
            "placeholders": placeholders,
            "note": "Use --force-apply to force API calls despite placeholders (not recommended).",
        }
        log.warning("Placeholders detected in bundle: %s. Skipping API calls.", placeholders)
        return False, msg

    # Build source + destination payloads consistent with generation logic
    src = bundle.get("source", {}) or {}
    dst = bundle.get("destination", {}) or {}
    src_payload = {
        "name": src.get("name", bundle.get("connection_name", "generated_src")),
        "definitionId": src.get("definitionId", "<<SOURCE_DEFINITION_ID>>"),
        "workspaceId": src.get("workspaceId", WORKSPACE_ID),
        "configuration": src.get("configuration", src.get("connectionConfiguration", {})),
    }
    dst_payload = {
        "name": dst.get("name", bundle.get("connection_name", "generated_dst")),
        "definitionId": dst.get("definitionId", "<<DEST_DEFINITION_ID>>"),
        "workspaceId": dst.get("workspaceId", WORKSPACE_ID),
        "configuration": dst.get("configuration", dst.get("connectionConfiguration", {})),
    }

    ok_src, src_id, src_err = check_or_create_source(src_payload)
    if not ok_src:
        return False, {"step": "create_source", "error": src_err, "payload": src_payload}

    ok_dst, dst_id, dst_err = check_or_create_destination(dst_payload)
    if not ok_dst:
        return False, {"step": "create_destination", "error": dst_err, "payload": dst_payload}

    conn_payload = {"name": bundle.get("connection_name"), "sourceId": src_id, "destinationId": dst_id}

    meta = bundle.get("metadata", {}) or {}
    requested_streams = (
        meta.get("requested_streams") or meta.get("requested_stream") or meta.get("stream") or meta.get("stream_name")
    )
    if requested_streams:
        if isinstance(requested_streams, list):
            streams = [{"name": s} for s in requested_streams]
        else:
            streams = [{"name": s.strip()} for s in str(requested_streams).split(",") if s.strip()]
    else:
        cfg_streams = bundle.get("configurations", {}).get("streams")
        if cfg_streams and isinstance(cfg_streams, list):
            streams = [{"name": s.get("name") if isinstance(s, dict) else str(s)} for s in cfg_streams]
        else:
            streams = [{"name": "<<STREAM_NAME>>"}]

    ok, streams_list, err = list_streams_for_pair(src_id, dst_id)
    if ok and streams_list:
        selected = []
        intent = bundle.get("connection_name", "").lower()

        for s in streams_list:
            name = s.get("streamName")
            if name and name.lower() in intent:
                selected.append(name)

        if not selected:
            selected = [s["streamName"] for s in streams_list]

        conn_payload["configurations"] = {
            "streams": [{"name": n} for n in selected]
        }

    created_ok, created_resp = create_connection(conn_payload)
    if not created_ok:
        return False, {"step": "create_connection", "error": created_resp, "payload": conn_payload}

    conn_id = None
    if isinstance(created_resp, dict):
        conn_id = extract_connection_id(created_resp)

    result = {"created": created_resp, "connectionId": conn_id}

    if run_sync and conn_id:
        ok_job, job_resp = trigger_sync(conn_id)
        result["job_ok"] = ok_job
        result["job"] = job_resp

    return True, result


# -----------------------
# Airbyte resource helpers
# -----------------------
def check_or_create_source(source_obj: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        r = airbyte_post("/sources", source_obj)
        if r.status_code in (200, 201):
            data = r.json()
            return True, data.get("sourceId") or data.get("id"), None
        return False, None, f"{r.status_code}: {r.text}"
    except Exception as exc:
        return False, None, str(exc)


def check_or_create_destination(dest_obj: Dict[str, Any]) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        r = airbyte_post("/destinations", dest_obj)
        if r.status_code in (200, 201):
            data = r.json()
            return True, data.get("destinationId") or data.get("id"), None
        return False, None, f"{r.status_code}: {r.text}"
    except Exception as exc:
        return False, None, str(exc)


def create_connection(conn_payload: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    try:
        r = airbyte_post("/connections", conn_payload)
        if r.status_code in (200, 201):
            return True, r.json()
        return False, {"status_code": r.status_code, "text": r.text}
    except Exception as exc:
        return False, {"error": str(exc)}


def list_streams_for_pair(source_id: str, destination_id: str) -> Tuple[bool, Optional[List[Dict[str, Any]]], Optional[str]]:
    """Query the streams endpoint for the given source/destination pair."""
    if not source_id or not destination_id:
        return False, None, "sourceId and destinationId required"
    try:
        path = "streams"
        params = {"sourceId": source_id, "destinationId": destination_id}
        r = airbyte_get(path, params=params)
        if r.status_code == 200:
            return True, r.json(), None
        return False, None, f"Status {r.status_code}: {r.text}"
    except Exception as exc:
        return False, None, str(exc)


# -----------------------
# Stream/catalog helpers
# -----------------------
def choose_stream(streams: List[Dict[str, Any]], preferred_name: Optional[str], user_intent: str) -> Optional[Dict[str, Any]]:
    """Pick an appropriate stream entry from the list using preference + intent heuristics."""
    if not streams:
        return None
    if preferred_name:
        for s in streams:
            if s.get("streamName", "").lower() == preferred_name.lower():
                return s

    lc_intent = (user_intent or "").lower()
    for s in streams:
        name = s.get("streamName", "")
        if name and name.lower() in lc_intent:
            return s

    if len(streams) == 1:
        return streams[0]

    return streams[0]


def map_destination_sync_mode(sync_mode_from_list: Optional[str]) -> str:
    """Map a sync mode token into Airbyte destinationSyncMode."""
    if not sync_mode_from_list:
        return "append"
    m = sync_mode_from_list.lower()
    if "overwrite" in m:
        return "overwrite"
    if "dedup" in m or "deduped" in m:
        return "deduped_history"
    return "append"


def build_sync_catalog_for_stream(
    stream_entry: Dict[str, Any],
    chosen_sync_mode: Optional[str] = None,
    chosen_cursor_field: Optional[List[str]] = None,
    chosen_primary_key: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Construct a minimal syncCatalog structure for a given stream entry."""
    if not stream_entry:
        return {"streams": []}

    stream_name = stream_entry.get("streamName")
    namespace = stream_entry.get("streamnamespace") or None
    default_cursor = stream_entry.get("defaultCursorField") or []
    source_pk = stream_entry.get("sourceDefinedPrimaryKey") or []

    cursor_field = chosen_cursor_field if chosen_cursor_field is not None else default_cursor
    primary_key = chosen_primary_key if chosen_primary_key is not None else source_pk

    sync_mode = chosen_sync_mode
    if not sync_mode:
        modes = stream_entry.get("syncModes") or []
        if cursor_field and any(m.startswith("incremental") for m in modes):
            found = next((m for m in modes if m.startswith("incremental")), None)
            sync_mode = found or (modes[0] if modes else "incremental_append")
        else:
            found = next((m for m in modes if m.startswith("full_refresh")), None)
            sync_mode = found or (modes[0] if modes else "full_refresh_append")

    destination_sync_mode = map_destination_sync_mode(sync_mode)

    stream_obj = {
        "stream": {
            "name": stream_name,
            "json_schema": {},
            **({"namespace": namespace} if namespace else {}),
        },
        "config": {
            "sync_mode": sync_mode,
            "destination_sync_mode": destination_sync_mode,
            "cursor_field": cursor_field or [],
            "primary_key": primary_key or [],
        },
    }

    return {"streams": [stream_obj]}




def extract_connection_id(created_resp: Dict[str, Any]) -> Optional[str]:
    """
    Extract connection id from varied Airbyte create responses.
    Returns first match or None.
    """
    if not created_resp or not isinstance(created_resp, dict):
        return None

    for key in ("connectionId", "id", "connection_id", "jobId"):
        val = created_resp.get(key)
        if isinstance(val, str):
            return val

    for candidate_key in ("connection", "item", "resource"):
        v = created_resp.get(candidate_key)
        if isinstance(v, dict):
            for key in ("connectionId", "id", "connection_id"):
                val = v.get(key)
                if isinstance(val, str):
                    return val

    if "job" in created_resp and isinstance(created_resp["job"], dict):
        j = created_resp["job"]
        if "id" in j:
            return str(j["id"])

    if "data" in created_resp and isinstance(created_resp["data"], dict):
        for key in ("connectionId", "id"):
            if key in created_resp["data"]:
                return created_resp["data"][key]

    def _scan(obj: Any) -> Optional[str]:
        if isinstance(obj, str):
            m = _UUID_RE.search(obj)
            if m:
                return m.group(0)
        if isinstance(obj, dict):
            for v in obj.values():
                r = _scan(v)
                if r:
                    return r
        if isinstance(obj, list):
            for item in obj:
                r = _scan(item)
                if r:
                    return r
        return None

    return _scan(created_resp)


# -----------------------
# Sync trigger helper
# -----------------------
def trigger_sync(connection_id: Optional[str]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Trigger a sync job for the given Airbyte connection id.
    Tries a couple of common job endpoints and returns (ok, response or error dict).
    """
    if not connection_id:
        return False, {"error": "empty connection_id"}

    body = {"jobType": "sync", "connectionId": connection_id}
    tried_resp = None

    for path in ("/jobs/create", "/jobs", "/v1/connections/sync", "/connections/sync"):
        try:
            r = airbyte_post(path, body)
            tried_resp = {"path": path, "status": r.status_code, "body": None}
            try:
                tried_resp["body"] = r.json()
            except Exception:
                tried_resp["body"] = r.text

            log.debug("trigger_sync: POST %s -> status=%s", _build_url(path), r.status_code)
            if r.status_code in (200, 201, 202):
                return True, tried_resp
        except Exception as exc:
            log.warning("trigger_sync path %s raised: %s", path, exc)
            tried_resp = {"path": path, "error": str(exc)}

    return False, {"error": "All attempted sync endpoints failed", "last": tried_resp}

import re

SYSTEM_WORDS = {
    "postgres", "postgresql",
    "teradata",
    "mysql",
    "oracle",
    "snowflake",
    "bigquery",
    "redshift",
    "s3",
    "kafka",
    "mongodb",
    "sqlserver",
    "db",
    "database",
    "table",
    "schema",
}


def extract_candidate_streams_from_intent(intent: str) -> list[str]:
    intent_lc = intent.lower()

    patterns = [
        r"ingest\s+([a-zA-Z_]+)",
        r"load\s+([a-zA-Z_]+)",
        r"sync\s+([a-zA-Z_]+)",
        r"copy\s+([a-zA-Z_]+)",
        r"([a-zA-Z_]+)\s+data",
        r"from\s+([a-zA-Z_]+)",
    ]

    found = []
    for p in patterns:
        for m in re.findall(p, intent_lc):
            if m not in SYSTEM_WORDS:
                found.append(m)

    return list(dict.fromkeys(found))


def resolve_streams_from_intent(user_intent: str, llm) -> list[str]:
    # Phase 1
    candidates = extract_candidate_streams_from_intent(user_intent)
    if candidates:
        return candidates

    # Phase 2 (LLM fallback)
    prompt = f"""
You are extracting SOURCE TABLE names.

Rules:
- If intent says "<name> data", assume table name "<name>"
- Return JSON array ONLY
- If nothing applies, return empty array []

Intent:
\"\"\"{user_intent}\"\"\"
"""
    resp = llm.invoke(prompt)
    text = getattr(resp, "content", "") or str(resp)

    try:
        return json.loads(text)
    except Exception:
        return []

def normalize_destination_schema(dest_entity: dict):
    cfg = dest_entity.get("configuration", {})
    if "schema" not in cfg or cfg.get("schema") == "airbyte_internal":
        cfg["schema"] = "<<TERADATA_SCHEMA>>"
    dest_entity["configuration"] = cfg
