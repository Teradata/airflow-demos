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
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from airbyte import is_airbyte_reachable, check_or_create_destination, check_or_create_source, list_streams_for_pair, \
    create_connection, apply_bundle_to_airbyte, resolve_streams_from_intent, normalize_destination_schema
from airflow import airflow_get_jwt_token, create_and_deploy_airflow_dag
from llm import get_embedding_and_chroma, build_prompt, get_chat_llm
from security import mask_sensitive
from util import _safe_filename, extract_json_from_text, contains_placeholders

# LangChain Bedrock bindings (assumed installed in the environment)

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("elt_manage")

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

def airflow_ensure_airbyte_connection_rest(
        conn_id: str = "airbyte_default",
        airbyte_url: str = None,
        client_id: str = None,
        client_secret: str = None,
        airflow_api: str = None,
        airflow_user: str = None,
        airflow_pass: str = None,
        overwrite: Optional[bool] = None,
):
    airflow_api = airflow_api or AIRFLOW_API
    airflow_user = airflow_user or AIRFLOW_USER
    airflow_pass = airflow_pass or AIRFLOW_PASS

    overwrite = overwrite if overwrite is not None else (
        os.getenv("AIRFLOW_OVERWRITE_CONN", "false").lower() == "true"
    )

    if not airflow_api:
        raise ValueError("AIRFLOW_API not set")

    # Get JWT token
    token = airflow_get_jwt_token(AIRFLOW_SERVER, airflow_user, airflow_pass)
    log.debug("JWT token acquired successfully.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    base_url = airflow_api.rstrip("/") + "/connections"
    conn_url = f"{base_url}/{conn_id}"

    # Check existing
    check = requests.get(conn_url, headers=headers)
    exists = check.status_code == 200

    if exists and not overwrite:
        log.info(f"✓ Connection '{conn_id}' already exists. Skipping creation.")
        return

    if exists and overwrite:
        log.debug(f"Deleting existing Airflow connection '{conn_id}'...")
        requests.delete(conn_url, headers=headers)

    # Payload
    payload = {
        "connection_id": conn_id,
        "conn_type": "airbyte",
        "host": airbyte_url,
        "login": client_id,
        "password": client_secret,
        "extra": json.dumps({"http_protocol": "http"})
    }

    body = {"connection": payload}

    log.debug(f"Creating new Airflow connection '{conn_id}'...")
    r = requests.post(base_url, headers=headers, json=body)

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Failed to create connection: {r.text}")

    log.debug("✓ Connection created successfully!")

    return {"status": "created", "skipped": False, "response": r.json()}





def generate_connection_from_intent(
    user_intent: str,
    k: int = RAG_TOP_K,
    max_attempts: int = MAX_REPAIR_ATTEMPTS,
    force_validate: Optional[bool] = None,
    auto_create: Optional[bool] = None,
    run_sync: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Generate connection bundle (optionally validate/create in Airbyte).
    Returns (connection_bundle, created_response_or_none_or_error).
    """
    if auto_create is None:
        auto_create = CREATE_IN_AIRBYTE_ENV

    emb, vs = get_embedding_and_chroma()
    results = vs.similarity_search_with_score(user_intent, k=k)
    retrieved = [(getattr(doc, "page_content", ""), getattr(doc, "metadata", {}), float(score)) for doc, score in results]
    prompt = build_prompt(retrieved, user_intent)
    llm = get_chat_llm(BEDROCK_LLM_ARN)

    if force_validate:
        validate = True
    elif force_validate is False:
        validate = False
    else:
        validate = bool(AIRBYTE_API_URL and is_airbyte_reachable(AIRBYTE_API_URL))
        if validate:
            log.debug("Airbyte detected and reachable at %s — validation ENABLED", AIRBYTE_API_URL)
        else:
            log.debug("Airbyte not detected or unreachable — running in GENERATE-ONLY mode")

    def has_sufficient_details_for_validation(entity: Dict[str, Any]) -> bool:
        if not isinstance(entity, dict):
            return False

        defid = entity.get("definitionId")
        if defid and isinstance(defid, str) and defid.strip() and not defid.strip().startswith("<<"):
            return True

        cfg = entity.get("configuration") or {}
        if not isinstance(cfg, dict):
            return False

        signature_keys = ["host", "database", "schema", "bucket", "account", "connectionString", "url"]
        for k_ in signature_keys:
            v = cfg.get(k_)
            if isinstance(v, str) and v.strip() and not v.strip().startswith("<<"):
                return True

        user = cfg.get("username") or cfg.get("user")
        pwd = cfg.get("password") or cfg.get("pwd")
        if user and isinstance(user, str) and user.strip() and not user.strip().startswith("<<"):
            if pwd and isinstance(pwd, str) and pwd.strip() and not pwd.strip().startswith("<<"):
                return True

        return False

    attempt = 0
    last_error: Optional[str] = None

    while attempt < max_attempts:
        attempt += 1
        log.debug("LLM generation attempt %d/%d", attempt, max_attempts)
        try:
            resp = llm.invoke(prompt)
            llm_text = getattr(resp, "content", None) or getattr(resp, "text", None) or str(resp)
        except Exception as exc:
            last_error = f"LLM call failed: {exc}"
            log.exception(last_error)
            time.sleep(1 + attempt)
            continue

        conn_bundle = extract_json_from_text(llm_text)
        if not conn_bundle:
            last_error = f"LLM output not parseable as JSON. Output excerpt:\n{(llm_text or '')[:1000]!s}"
            log.warning(last_error)
            prompt += "\n\nNOTE: Your previous output was not valid JSON. Return ONLY the JSON object."
            continue

        log.debug("Generated bundle keys: %s", list(conn_bundle.keys()))

        required_keys = {"connection_name", "source", "destination", "sync"}
        if not required_keys.issubset(set(conn_bundle.keys())):
            missing = required_keys - set(conn_bundle.keys())
            last_error = f"Missing required keys: {missing}"
            log.warning(last_error)
            prompt += f"\n\nYour JSON is missing required keys: {missing}. Return corrected JSON only."
            continue

        src_entity = conn_bundle.get("source", {}) or {}
        dst_entity = conn_bundle.get("destination", {}) or {}

        # If placeholders exist in either entity, do NOT attempt live validation/creation.
        if contains_placeholders(src_entity) or contains_placeholders(dst_entity):
            normalize_destination_schema(dst_entity)
            conn_bundle["destination"] = dst_entity
            resolved_streams = resolve_streams_from_intent(user_intent, llm)

            if not resolved_streams:
                log.warning("No streams resolved from intent; bundle will be incomplete")

            conn_bundle.setdefault("configurations", {})
            conn_bundle["configurations"]["streams"] = [
                {"name": s} for s in resolved_streams
            ]

            return conn_bundle, None

        if validate:
            src_has = has_sufficient_details_for_validation(src_entity)
            dst_has = has_sufficient_details_for_validation(dst_entity)
            if not (src_has and dst_has):
                log.debug(
                    "Insufficient concrete details detected for validation (src_has=%s dst_has=%s). Skipping Airbyte API validation and returning generated bundle.",
                    src_has,
                    dst_has,
                )
                if isinstance(src_entity, dict) and not src_entity.get("workspaceId"):
                    src_entity["workspaceId"] = WORKSPACE_ID
                    conn_bundle["source"] = src_entity
                if isinstance(dst_entity, dict) and not dst_entity.get("workspaceId"):
                    dst_entity["workspaceId"] = WORKSPACE_ID
                    conn_bundle["destination"] = dst_entity
                normalize_destination_schema(conn_bundle["destination"])
                return conn_bundle, None

        if not validate:
            log.debug("Validation disabled — returning generated connection bundle (no API checks).")
            return conn_bundle, None

        src = conn_bundle["source"].copy()
        dst = conn_bundle["destination"].copy()

        src_payload = {
            "name": src.get("name", conn_bundle["connection_name"] + "_src"),
            "definitionId": src.get("definitionId", "<<SOURCE_DEFINITION_ID>>"),
            "workspaceId": src.get("workspaceId", WORKSPACE_ID),
            "configuration": src.get("configuration", src.get("connectionConfiguration", {})),
        }
        dst_payload = {
            "name": dst.get("name", conn_bundle["connection_name"] + "_dst"),
            "definitionId": dst.get("definitionId", "<<DEST_DEFINITION_ID>>"),
            "workspaceId": dst.get("workspaceId", WORKSPACE_ID),
            "configuration": dst.get("configuration", dst.get("connectionConfiguration", {})),
        }

        ok_src, src_id, src_err = check_or_create_source(src_payload)
        if not ok_src:
            last_error = f"Source create/check failed: {src_err}"
            log.warning(last_error)
            prompt += f"\n\nAirbyte source create/check failed with error:\n{src_err}\nPlease return corrected connection_bundle JSON only."
            continue

        ok_dst, dst_id, dst_err = check_or_create_destination(dst_payload)
        if not ok_dst:
            last_error = f"Destination create/check failed: {dst_err}"
            log.warning(last_error)
            prompt += f"\n\nAirbyte destination create/check failed with error:\n{dst_err}\nPlease return corrected connection_bundle JSON only."
            continue

        conn_payload: Dict[str, Any] = {
            "name": conn_bundle.get("connection_name"),
            "sourceId": src_id,
            "destinationId": dst_id,
        }
        log.debug("conn_payload - %s ", conn_payload)
        def normalize_requested_list(val: Any) -> List[str]:
            if not val:
                return []
            if isinstance(val, list):
                return [str(x).strip() for x in val if str(x).strip()]
            if isinstance(val, str):
                return [s.strip() for s in val.split(",") if s.strip()]
            return []

        # -----------------------
        # Resolve requested streams
        # -----------------------
        meta = conn_bundle.get("metadata", {}) or {}
        log.debug("meta - %s ", meta)
        requested_streams = normalize_requested_list(
            meta.get("requested_streams")
            or meta.get("requested_stream")
            or meta.get("stream")
            or meta.get("stream_name")
        )

        if requested_streams:
            log.debug("Requested streams from metadata: %s", requested_streams)

        ok_streams, streams_list, streams_err = list_streams_for_pair(src_id, dst_id)

        log.debug("streams_list: %s", streams_list)

        selected_names: List[str] = []

        if ok_streams and isinstance(streams_list, list) and streams_list:
            available: List[str] = []

            for s in streams_list:
                stream_name = s.get("streamName") or s.get("name")
                if not stream_name:
                    continue

                available.append(stream_name)




            log.debug("requested_streams: %s", requested_streams)

            # 1️⃣ Explicit user-requested streams win
            if requested_streams:
                for req in requested_streams:
                    match = next((a for a in available if a.lower() == req.lower()), None)
                    if not match:
                        match = next((a for a in available if req.lower() in a.lower()), None)

                    if match:
                        selected_names.append(match)
                    else:
                        log.warning("Requested stream '%s' not found; skipping.", req)

            # 2️⃣ Intent-based inference fallback
            else:
                lc_intent = (user_intent or "").lower()
                for a in available:
                    variants = {
                        a.lower(),
                        a.lower().replace("_", " "),
                        a.lower().replace("-", " "),
                    }
                    if a.endswith("s"):
                        variants.add(a[:-1].lower())
                    else:
                        variants.add(f"{a}s".lower())

                    if any(v in lc_intent for v in variants):
                        selected_names.append(a)

            # 3️⃣ Deduplicate
            selected_names = list(dict.fromkeys(selected_names))

            # 4️⃣ Final fallback
            if not selected_names and available:
                selected_names.append(available[0])

        else:
            log.warning("Could not list streams for source/destination: %s", streams_err)
            selected_names = requested_streams[:] if requested_streams else ["<<STREAM_NAME>>"]

        log.debug("Selected streams for connection: %s", selected_names)

        conn_payload["configurations"] = {
            "streams": [{"name": name} for name in selected_names]
        }

        # -----------------------
        # Create connection
        # -----------------------
        created_ok, created_resp = create_connection(conn_payload)

        # -----------------------
        # Generate dbt project ONLY after successful creation
        # -----------------------
        if created_ok and run_sync:
            log.debug("Connection created in Airbyte: %s", created_resp)

            dest = conn_bundle.get("destination", {})
            cfg = dest.get("configuration", {})
            logmech = cfg.get("logmech", {})


            try:
                airflow_ensure_airbyte_connection_rest(
                    conn_id="airbyte_default",
                    airbyte_url=AIRBYTE_API_URL,
                    client_id=os.getenv("AIRBYTE_CLIENT_ID"),
                    client_secret=os.getenv("AIRBYTE_CLIENT_SECRET"),
                    airflow_api=AIRFLOW_API,
                    airflow_user=AIRFLOW_USER,
                    airflow_pass=AIRFLOW_PASS,
                )
            except Exception as e:
                log.debug("Failed to ensure Airflow connection:", e)

            # conn_bundle is your generated bundle, created_resp is create_connection response
            try:
                dag_result = create_and_deploy_airflow_dag( conn_bundle, created_resp)
                # Optionally add DAG info into metadata for future cleanup/upserts:
                if dag_result.get("ok"):
                    conn_bundle.setdefault("metadata", {})["airflow_dag"] = dag_result
            except Exception as exc:
                log.warning("Failed to create/deploy Airflow DAG: %s", exc)

            conn_bundle.setdefault("configurations", {})
            conn_bundle["configurations"]["streams"] = [
                {"name": name} for name in selected_names
            ]

            return conn_bundle, created_resp

        last_error = f"Connection create failed: {created_resp}"
        log.warning(last_error)
        prompt += f"\n\nAirbyte connections/create returned error:\n{created_resp}\nPlease return corrected connection_bundle JSON only."

    log.error(
        "Failed to generate a validated connection bundle after %d attempts. Last error: %s",
        max_attempts,
        last_error,
    )
    return None, {"error": last_error}


# -----------------------
# CLI entrypoint
# -----------------------
def _parse_args() -> Tuple[Optional[str], int, int, Optional[bool], Optional[bool], Optional[str], bool, bool, bool]:
    import argparse

    parser = argparse.ArgumentParser(description="Generate Airbyte connection bundle (RAG + Bedrock ARN LLM).")
    parser.add_argument("--query", "-q", required=False, help="Natural language intent")
    parser.add_argument("--k", type=int, default=RAG_TOP_K, help="Top-k retrieval")
    parser.add_argument("--attempts", type=int, default=MAX_REPAIR_ATTEMPTS, help="Max repair attempts")
    parser.add_argument("--create", action="store_true", help="Also call /connections/create when validated")
    parser.add_argument("--no-validate", action="store_true", help="Force skip Airbyte validation even if AIRBYTE_API_URL is set")
    parser.add_argument("--validate", action="store_true", help="Force validation (error if Airbyte unreachable)")
    parser.add_argument("--apply-bundle", help="Path to existing connection_bundle JSON to apply directly")
    parser.add_argument("--force-apply", action="store_true", help="Force application even if placeholders are present")
    parser.add_argument("--run-sync", "-s", action="store_true", help="Trigger a sync job for the created connection")
    parser.add_argument(
        "--save-bundle",
        metavar="FILE",
        help="Save the generated connection_bundle to a JSON file"
    )

    args = parser.parse_args()

    auto_create = args.create if args.create else None
    if args.no_validate:
        force_validate = False
    elif args.validate:
        force_validate = True
    else:
        force_validate = None

    env_run = RUN_SYNC.lower() in ("1", "true", "yes")
    run_sync_flag = args.run_sync or env_run

    return args.query, args.k, args.attempts, force_validate, auto_create, args.apply_bundle, args.force_apply, run_sync_flag, args.save_bundle


if __name__ == "__main__":
    # parse args (new signature)
    query, k, attempts, force_validate, auto_create, apply_bundle_path, force_apply, run_sync_flag, save_bundle = _parse_args()

    if apply_bundle_path:
        # load and apply existing bundle (skip LLM/RAG)
        try:
            with open(apply_bundle_path, "r", encoding="utf-8") as fh:
                bundle = json.load(fh)
        except Exception as exc:
            log.error("Failed to load bundle JSON: %s", exc)
            raise SystemExit(2)

        ok, resp = apply_bundle_to_airbyte(bundle, run_sync=run_sync_flag, force_apply=force_apply)
        log.info(json.dumps({"ok": ok, "resp": resp}, indent=2, default=str))
        raise SystemExit(0)

    # existing generation code path (unchanged)
    if not query:
        log.error("No query provided for generation. Use --query or --apply-bundle.")
        raise SystemExit(2)

    bundle, created = generate_connection_from_intent(
        query,
        k=k,
        max_attempts=attempts,
        force_validate=force_validate,
        auto_create=auto_create,
        run_sync=run_sync_flag,
    )

    if bundle:
        log.debug("\n=== Final connection bundle ===")
        log.info(json.dumps(mask_sensitive(bundle), indent=2))
        if save_bundle:
            try:
                with open(save_bundle, "w", encoding="utf-8") as f:
                    json.dump(bundle, f, indent=2)
                log.debug(f"\nBundle saved to {save_bundle}")
            except Exception as exc:
                log.error(f"\nFailed to save bundle: {exc}")
        else:
            # --- Save bundle to file ---
            output_file = "generated_bundle.json"
            try:
                with open(output_file, "w", encoding="utf-8") as f:
                    json.dump(bundle, f, indent=2)
                log.info(f"\nBundle saved to {output_file}")
            except Exception as exc:
                log.error(f"\nFailed to save bundle: {exc}")
            if created:
                log.debug("\n=== Airbyte create/response ===")
                log.debug(json.dumps(created, indent=2))

    else:
        log.error("\nFailed to generate validated connection bundle. See logs for details.")




