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
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
# LangChain Bedrock bindings (assumed installed in the environment)
from langchain_aws import BedrockEmbeddings, ChatBedrock
from langchain_chroma import Chroma

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_connection")

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

if not BEDROCK_LLM_ARN:
    log.error("Environment variable BEDROCK_LLM_ARN must be set to your Bedrock inference profile ARN.")
    raise SystemExit(1)

# -----------------------
# Constants & templates
# -----------------------
PROMPT_TEMPLATE = """
You are Airbyte Connector Agent. Produce a single JSON object ONLY (no explanation).
It must be valid JSON and follow this structure:

{
  "connection_name": "<string>",
  "source": {
     "name": "<string>",
     "definitionId": "<uuid_or_placeholder>",
     "workspaceId": "<<WORKSPACE_ID>>",
     "configuration": {
       "sourceType": "<connector_type>"
       /* other configuration fields */
     }
  },
  "destination": {
     "name": "<string>",
     "definitionId": "<uuid_or_placeholder>",
     "workspaceId": "<<WORKSPACE_ID>>",
     "configuration": {
       "destinationType": "<connector_type>"
       /* other configuration fields */
     }
  },
  "sync": {
     "sync_mode": "incremental" | "full_refresh",
     "cursor_field": "<string or null>",
     "schedule": { "type": "cron", "cronExpression": "<cron_expr>" }
  },
  "metadata": { "created_by": "aca_agent", "notes": "<string>" }
}

Guidelines:
- Use retrieved docs to populate connector fields.
- Use placeholders for secrets like "<<PASSWORD>>".
- If unsure about cursor_field existence, set it to null and explain in metadata.notes.
- OUTPUT ONLY the single JSON object.
- while generating source and destination configuration, refer to connector _spec.json files

IMPORTANT TERADATA SCHEMA RULES — FOLLOW EXACTLY:

When the connector is Teradata (destination or source), the connector must include a JSON
object property named "logmech" inside the connectionConfiguration (or configuration).
This "logmech" must be an object and conform exactly to one of the two schemas below.

Schema A (TD2):
{
  "logmech": {
    "auth_type": "TD2",
    "username": "<username>",
    "password": "<password>"
  }
}

Schema B (LDAP):
{
  "logmech": {
    "auth_type": "LDAP",
    "username": "<username>",
    "password": "<password>"
  }
}

Rules:
- Do NOT output "logmech" as a string.
- Use exactly "logmech" and inside it exactly "auth_type", "username", "password".
- Use placeholders for secrets (e.g., "<<TERADATA_PASS>>") unless the user provided real secrets.
- OUTPUT ONLY the final JSON object (do not wrap it in any extra keys or text).
- Read definitionId from source and destination metadata.yaml files when possible.

Retrieved context:
<<RETRIEVED_CONTEXT>>

User intent:
\"\"\"<<USER_INTENT>>\"\"\"
"""

# -----------------------
# Utility helpers
# -----------------------
def retry(fn, *args, retries: int = 3, backoff_base: float = 0.5, **kwargs):
    """Simple retry helper with exponential backoff."""
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == retries:
                log.exception("Operation failed after %d attempts", attempt)
                raise
            wait = backoff_base * (2 ** (attempt - 1)) * (1 + random.random() * 0.1)
            log.warning("Attempt %d failed: %s — retrying in %.2fs", attempt, exc, wait)
            time.sleep(wait)
    return None


def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    """
    Try to extract a JSON object from arbitrary LLM text. Returns dict or None.
    This tries a direct parse first, then progressively trims trailing characters.
    """
    if not text:
        return None

    # fast path: try parse whole text
    try:
        return json.loads(text)
    except Exception:
        pass

    start = text.find("{")
    if start == -1:
        return None

    # attempt progressively smaller substrings (works for trailing text)
    for end in range(len(text), start, -1):
        try:
            candidate = text[start:end]
            return json.loads(candidate)
        except Exception:
            continue

    return None


# -----------------------
# LLM / Retriever setup
# -----------------------
def get_embedding_and_chroma(persist_dir: str = CHROMA_PERSIST_DIR, embed_model: str = BEDROCK_EMBED_MODEL):
    """Initialize Bedrock embeddings and Chroma vector store."""
    log.info("Initializing BedrockEmbeddings (%s) and Chroma (dir=%s)", embed_model, persist_dir)
    emb = BedrockEmbeddings(region_name=AWS_REGION, model_id=embed_model)
    vs = Chroma(persist_directory=str(persist_dir), collection_name=COLLECTION_NAME, embedding_function=emb)
    return emb, vs


def get_chat_llm(arn: str):
    """Initialize ChatBedrock LLM wrapper."""
    provider = os.getenv("BEDROCK_PROVIDER", "anthropic")
    model_kwargs = {
        "temperature": float(os.getenv("BEDROCK_TEMPERATURE", "0.0")),
        "max_tokens": int(os.getenv("BEDROCK_MAX_TOKENS", "1024")),
    }
    log.info("Initializing ChatBedrock with ARN=%s provider=%s", arn, provider)
    return ChatBedrock(model_id=arn, provider=provider, model_kwargs=model_kwargs)


def build_prompt(retrieved: List[Tuple[str, dict, float]], user_intent: str) -> str:
    """Assemble prompt from template, retrieved docs and user intent."""
    ctx_parts: List[str] = []
    for i, (text, md, score) in enumerate(retrieved):
        src = md.get("source", f"doc_{i}")
        ctx_parts.append(f"---\nSource: {src}\nScore: {score}\n\n{text}\n")
    retrieved_context = "\n".join(ctx_parts)

    prompt = PROMPT_TEMPLATE.replace("<<RETRIEVED_CONTEXT>>", retrieved_context)
    prompt = prompt.replace("<<USER_INTENT>>", user_intent or "")
    prompt = prompt.replace("<<WORKSPACE_ID>>", WORKSPACE_ID)
    return prompt


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
    log.info("airbyte post request - %s %s", url, body)
    return requests.post(url, json=body, headers=headers, timeout=timeout)


def airbyte_get(path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 30) -> requests.Response:
    """GET wrapper for Airbyte endpoints."""
    url = _build_url(path)
    headers = {"Accept": "application/json"}
    if AIRBYTE_API_TOKEN:
        headers["Authorization"] = f"Bearer {AIRBYTE_API_TOKEN}"
    log.info("airbyte get request - %s params=%s", url, params)
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


def contains_placeholders(entity: dict) -> bool:
    """
    Return True if any string value in the entity (recursively in configuration)
    looks like a placeholder token, e.g. starts with '<<' or contains '<<...>>'.
    Works for dicts/lists/strings. Non-string values ignored.
    """
    if not isinstance(entity, dict):
        return False

    def _scan(val) -> bool:
        if isinstance(val, str):
            # common placeholder patterns used in this project
            s = val.strip()
            if s.startswith("<<") and s.endswith(">>"):
                return True
            if "<<" in s and ">>" in s:
                return True
            return False
        if isinstance(val, dict):
            for v in val.values():
                if _scan(v):
                    return True
        if isinstance(val, list):
            for item in val:
                if _scan(item):
                    return True
        return False

    # Scan top-level definitionId and configuration/connectionConfiguration as well
    defid = entity.get("definitionId")
    if isinstance(defid, str) and _scan(defid):
        return True

    # Check configurations under common keys
    for cfg_key in ("configuration", "connectionConfiguration"):
        cfg = entity.get(cfg_key)
        if cfg and _scan(cfg):
            return True

    # fallback: scan whole entity body
    return _scan(entity)


PLACEHOLDER_RE = re.compile(r"<<\s*([^<> \t\n\r]+)\s*>>")


def find_placeholders_in_bundle(bundle: Dict[str, Any]) -> List[str]:
    """
    Return list of unique placeholder token names found in the bundle (without << >>).
    """
    found = set()

    def _scan(obj: Any):
        if isinstance(obj, str):
            for m in PLACEHOLDER_RE.findall(obj):
                found.add(m)
        elif isinstance(obj, dict):
            for v in obj.values():
                _scan(v)
        elif isinstance(obj, list):
            for item in obj:
                _scan(item)

    _scan(bundle)
    return sorted(found)


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

    conn_payload["configurations"] = {"streams": streams}

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


# -----------------------
# Connection id extraction
# -----------------------
_UUID_RE = re.compile(r"[0-9a-fA-F\-]{36}")


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

            log.info("trigger_sync: POST %s -> status=%s", _build_url(path), r.status_code)
            if r.status_code in (200, 201, 202):
                return True, tried_resp
        except Exception as exc:
            log.warning("trigger_sync path %s raised: %s", path, exc)
            tried_resp = {"path": path, "error": str(exc)}

    return False, {"error": "All attempted sync endpoints failed", "last": tried_resp}


# -----------------------
# Main generator (unchanged semantics)
# -----------------------
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
            log.info("Airbyte detected and reachable at %s — validation ENABLED", AIRBYTE_API_URL)
        else:
            log.info("Airbyte not detected or unreachable — running in GENERATE-ONLY mode")

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
        log.info("LLM generation attempt %d/%d", attempt, max_attempts)
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

        log.info("Generated bundle keys: %s", list(conn_bundle.keys()))

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
            log.info(
                "Placeholders detected in generated bundle (src_or_dst). Skipping Airbyte validation/create and returning bundle with placeholders."
            )
            if isinstance(src_entity, dict) and not src_entity.get("workspaceId"):
                src_entity["workspaceId"] = WORKSPACE_ID
                conn_bundle["source"] = src_entity
            if isinstance(dst_entity, dict) and not dst_entity.get("workspaceId"):
                dst_entity["workspaceId"] = WORKSPACE_ID
                conn_bundle["destination"] = dst_entity
            return conn_bundle, None

        if validate:
            src_has = has_sufficient_details_for_validation(src_entity)
            dst_has = has_sufficient_details_for_validation(dst_entity)
            if not (src_has and dst_has):
                log.info(
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
                return conn_bundle, None

        if not validate:
            log.info("Validation disabled — returning generated connection bundle (no API checks).")
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

        def normalize_requested_list(val: Any) -> List[str]:
            if not val:
                return []
            if isinstance(val, list):
                return [str(x).strip() for x in val if x]
            if isinstance(val, str):
                return [s.strip() for s in val.split(",") if s.strip()]
            return []

        meta = conn_bundle.get("metadata", {}) or {}
        requested_streams = normalize_requested_list(
            meta.get("requested_streams")
            or meta.get("requested_stream")
            or meta.get("stream")
            or meta.get("stream_name")
        )
        if requested_streams:
            log.info("Requested streams from metadata: %s", requested_streams)

        ok_streams, streams_list, streams_err = list_streams_for_pair(src_id, dst_id)

        selected_names: List[str] = []

        if ok_streams and isinstance(streams_list, list) and len(streams_list) > 0:
            available: List[str] = []
            for s in streams_list:
                nm = s.get("streamName") or s.get("name") or ""
                if nm:
                    available.append(nm)

            log.info("Available streams from Airbyte: %s", available)

            if requested_streams:
                for req in requested_streams:
                    match = next((a for a in available if a.lower() == req.lower()), None)
                    if not match:
                        match = next((a for a in available if req.lower() in a.lower()), None)
                    if match:
                        selected_names.append(match)
                    else:
                        log.warning("Requested stream '%s' not found in available streams; skipping.", req)
            else:
                lc_intent = (user_intent or "").lower()
                for a in available:
                    a_variants = {a.lower(), a.lower().replace("_", " ").replace("-", " ")}
                    if a.endswith("s"):
                        a_variants.add(a[:-1].lower())
                    else:
                        a_variants.add((a + "s").lower())

                    if any(v in lc_intent for v in a_variants):
                        selected_names.append(a)

            seen = set()
            selected_names = [x for x in selected_names if not (x in seen or seen.add(x))]

            if not selected_names:
                lc_intent = (user_intent or "").lower()
                for special in ("customer", "commits"):
                    if special in lc_intent:
                        match = next((a for a in available if a.lower() == special), None)
                        if match and match not in selected_names:
                            selected_names.append(match)

            if not selected_names and available:
                selected_names.append(available[0])
        else:
            log.warning("Could not list streams for source/destination: %s", streams_err)
            if requested_streams:
                selected_names = requested_streams[:]
            else:
                selected_names = ["<<STREAM_NAME>>"]

        log.info("Selected streams for connection: %s", selected_names)
        conn_payload["configurations"] = {"streams": [{"name": name} for name in selected_names]}

        created_ok, created_resp = create_connection(conn_payload)

        if created_ok:
            log.info("Connection created in Airbyte: %s", created_resp)
            conn_id = extract_connection_id(created_resp if isinstance(created_resp, dict) else {})
            if run_sync:
                ok_job, job_resp = trigger_sync(conn_id)
                if ok_job:
                    log.info("Triggered sync job for connection %s: %s", conn_id, job_resp)
                else:
                    log.warning("Failed to trigger sync job for connection %s: %s", conn_id, job_resp)
                return conn_bundle, {"created": created_resp, "job": job_resp}
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
        print(json.dumps({"ok": ok, "resp": resp}, indent=2, default=str))
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
        print("\n=== Final connection bundle ===")
        print(json.dumps(bundle, indent=2))
        if save_bundle:
            try:
                with open(save_bundle, "w") as f:
                    json.dump(bundle, f, indent=2)
                print(f"\nBundle saved to {save_bundle}")
            except Exception as exc:
                print(f"\nFailed to save bundle: {exc}")
        else:
            # --- Save bundle to file ---
            output_file = "generated_bundle.json"
            try:
                with open(output_file, "w") as f:
                    json.dump(bundle, f, indent=2)
                print(f"\nBundle saved to {output_file}")
            except Exception as exc:
                print(f"\nFailed to save bundle: {exc}")
            if created:
                print("\n=== Airbyte create/response ===")
                print(json.dumps(created, indent=2))

    else:
        print("\nFailed to generate validated connection bundle. See logs for details.")
