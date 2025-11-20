#!/usr/bin/env python3
"""
generate_connection.py (dual-mode: with or without Airbyte validation)

- Uses LangChain ChatBedrock (Inference Profile ARN) for LLM.
- Uses BedrockEmbeddings (Titan) + Chroma for RAG retrieval.
- If Airbyte API is configured and reachable (and validation enabled), performs:
    discover/create source/destination, connections/check_connection, optional create.
- If Airbyte is not configured/unreachable or validation disabled, returns generated JSON only.
- CLI flags to control behavior.

Environment variables (recommended):
  AWS_REGION (default: us-west-2)
  BEDROCK_EMBED_MODEL (default: amazon.titan-embed-text-v2:0)
  BEDROCK_LLM_ARN (REQUIRED)
  AIRBYTE_API_URL (optional)
  AIRBYTE_API_TOKEN (optional, Bearer)
  CREATE_IN_AIRBYTE (optional, true/false default false)
  MAX_REPAIR_ATTEMPTS (default 3)
  RAG_TOP_K (default 6)
  CHROMA_PERSIST_DIR (default ./chroma_store/current)
"""

import os
import json
import logging
import time
import random
from typing import List, Tuple, Optional
from pathlib import Path

from dotenv import load_dotenv
import requests

# LangChain Bedrock bindings
from langchain_aws import BedrockEmbeddings, ChatBedrock
from langchain_chroma import Chroma

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("generate_connection")

# Load .env for local dev
load_dotenv()

# -----------------------
# Config
# -----------------------
AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
BEDROCK_EMBED_MODEL = os.getenv("BEDROCK_EMBED_MODEL", "amazon.titan-embed-text-v2:0")
BEDROCK_LLM_ARN = os.getenv("BEDROCK_LLM_ARN")  # required
AIRBYTE_API_URL = os.getenv("AIRBYTE_API_URL", "").strip()
AIRBYTE_API_TOKEN = os.getenv("AIRBYTE_API_TOKEN", "").strip()
CREATE_IN_AIRBYTE_ENV = os.getenv("CREATE_IN_AIRBYTE", "false").lower() in ("1", "true", "yes")
MAX_REPAIR_ATTEMPTS = int(os.getenv("MAX_REPAIR_ATTEMPTS", "3"))
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "6"))
CHROMA_PERSIST_DIR = os.getenv("CHROMA_PERSIST_DIR", "./chroma_store/current")
COLLECTION_NAME = os.getenv("CHROMA_COLLECTION", "airbyte_connectors")
WORKSPACE_ID = os.getenv("AIRBYTE_WORKSPACE_ID", "<<WORKSPACE_ID>>")

if not BEDROCK_LLM_ARN:
    log.error("Environment variable BEDROCK_LLM_ARN must be set to your Bedrock inference profile ARN.")
    raise SystemExit(1)

# -----------------------
# Helpers
# -----------------------
def retry(func, *args, retries=3, backoff_base=0.5, **kwargs):
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == retries:
                log.exception("Operation failed after %d attempts", attempt)
                raise
            wait = backoff_base * (2 ** (attempt - 1)) * (1 + random.random() * 0.1)
            log.warning("Attempt %d failed: %s. Retrying in %.2fs", attempt, e, wait)
            time.sleep(wait)

def extract_json_from_text(text: str) -> Optional[dict]:
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    for end in range(len(text), start, -1):
        try:
            candidate = text[start:end]
            return json.loads(candidate)
        except Exception:
            continue
    try:
        return json.loads(text)
    except Exception:
        return None

# -----------------------
# Retriever & LLM setup
# -----------------------
def get_embedding_and_chroma(persist_dir: str = CHROMA_PERSIST_DIR, embed_model: str = BEDROCK_EMBED_MODEL):
    log.info("Initializing BedrockEmbeddings (%s) and Chroma (dir=%s)", embed_model, persist_dir)
    emb = BedrockEmbeddings(region_name=AWS_REGION, model_id=embed_model)
    vs = Chroma(persist_directory=str(persist_dir), collection_name=COLLECTION_NAME, embedding_function=emb)
    return emb, vs

def get_chat_llm(arn: str):
    provider = os.getenv("BEDROCK_PROVIDER", "anthropic")
    model_kwargs = {"temperature": float(os.getenv("BEDROCK_TEMPERATURE", "0.0")), "max_tokens": int(os.getenv("BEDROCK_MAX_TOKENS", "1024"))}
    log.info("Initializing ChatBedrock with ARN=%s provider=%s", arn, provider)
    return ChatBedrock(model_id=arn, provider=provider, model_kwargs=model_kwargs)

# -----------------------
# Prompt template
# -----------------------
WORKSPACE_ID = os.getenv("AIRBYTE_WORKSPACE_ID", "<<WORKSPACE_ID>>")
# Replace PROMPT_TEMPLATE with this (note: tokens <<RETRIEVED_CONTEXT>> and <<USER_INTENT>>)
PROMPT_TEMPLATE = """
You are Airbyte Connector Agent. Produce a single JSON object named "connection_bundle" ONLY (no explanation).
It must be valid JSON and follow this structure:

{
  "connection_name": "<string>",
  "source": {
     "name": "<string>",
     "definitionId": "<uuid_or_placeholder>",
     "workspaceId": "<<WORKSPACE_ID>>",
     "configuration": {
       "sourceType": "<connector_type>",
       ... other config fields ...
   }
  },
  "destination": {
     "name": "<string>",
     "definitionId": "<uuid_or_placeholder>",
     "workspaceId": "<<WORKSPACE_ID>>",
      "configuration": {
       "destinationType": "<connector_type>",
       ... other config fields ...
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
- while generating source and destination configuration, refer _spec.json files of respective connector

IMPORTANT TERADATA SCHEMA RULES — FOLLOW EXACTLY:

When the connector is Teradata (destination or source), the connector **must** include a JSON object property named "logmech" inside the connectionConfiguration (or configuration). This "logmech" must be an **object**, not a string, and it must conform to exactly one of the two schemas below (use the exact property names and values, do not invent synonyms):

Schema A (TD2):
{
  "logmech": {
    "auth_type": "TD2",      // must be the literal string "TD2"
    "username": "<username>",// required string
    "password": "<password>" // required string (placeholder allowed)
  }
}

Schema B (LDAP):
{
  "logmech": {
    "auth_type": "LDAP",     // must be the literal string "LDAP"
    "username": "<username>",// required string
    "password": "<password>" // required string (placeholder allowed)
  }
}

Rules:
- Do NOT output "logmech" as a string (e.g. "logmech": "TD2") — it's invalid.
- Do NOT use alternative keys like "logon_mech", "logon_mechanism", "logMech". Use exactly "logmech" and inside it exactly "auth_type", "username", "password".
- The LLM must ensure the chosen "logmech" object matches **one and only one** of the two schemas above (i.e., include required username and password).
- Use placeholders for secrets (e.g., "<<TERADATA_PASS>>") unless the user provided real secrets in the intent.
- Use canonical Airbyte API keys in the final JSON: destinationDefinitionId (not definitionId), workspaceId, connectionConfiguration (not configuration).
- OUTPUT ONLY the final JSON object named "connection_bundle".
- Read definitionId from source and destination metadata.yaml files 

If you are unsure, prefer to output the required fields with placeholders rather than omit them.

Retrieved context:
<<RETRIEVED_CONTEXT>>

User intent:
\"\"\"<<USER_INTENT>>\"\"\"
"""

# Replace build_prompt with this simple token-replace version
def build_prompt(retrieved: List[Tuple[str, dict, float]], user_intent: str) -> str:
    ctx_parts = []
    for i, (text, md, score) in enumerate(retrieved):
        src = md.get("source", f"doc_{i}")
        ctx_parts.append(f"---\nSource: {src}\nScore: {score}\n\n{text}\n")
    retrieved_context = "\n".join(ctx_parts)

    prompt = PROMPT_TEMPLATE.replace("<<RETRIEVED_CONTEXT>>", retrieved_context)
    prompt = prompt.replace("<<USER_INTENT>>", user_intent or "")
    # substitute workspace id token if you like
    prompt = prompt.replace("<<WORKSPACE_ID>>", WORKSPACE_ID)
    return prompt

# -----------------------
# Airbyte helpers
# -----------------------
def is_airbyte_reachable(api_url: str, timeout: float = 5.0) -> bool:
    if not api_url:
        return False
    health_url = api_url.rstrip("/") + "/health"  # many Airbyte versions expose /health; fallback to root
    try:
        r = requests.get(health_url, timeout=timeout)
        if r.status_code == 200:
            return True
    except Exception:
        pass
    # fallback: try a lightweight endpoint
    try:
        r = requests.get(api_url.rstrip("/") + "/sources/list", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False

def airbyte_post(path: str, body: dict, timeout: int = 30):
    log.info("airbyte post request - %s", body)
    base = AIRBYTE_API_URL.rstrip("/") if AIRBYTE_API_URL else ""
    url = base + path if path.startswith("/") else base + "/" + path
    headers = {"Content-Type": "application/json"}
    if AIRBYTE_API_TOKEN:
        headers["Authorization"] = f"Bearer {AIRBYTE_API_TOKEN}"
    return requests.post(url, json=body, headers=headers, timeout=timeout)

def check_or_create_source(source_obj: dict) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        r = airbyte_post("/sources", source_obj)
        if r.status_code in (200, 201):
            data = r.json()
            return True, data.get("sourceId") or data.get("id"), None
        return False, None, f"{r.status_code}: {r.text}"
    except Exception as e:
        return False, None, str(e)

def check_or_create_destination(dest_obj: dict) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        r = airbyte_post("/destinations", dest_obj)
        if r.status_code in (200, 201):
            data = r.json()
            return True, data.get("destinationId") or data.get("id"), None
        return False, None, f"{r.status_code}: {r.text}"
    except Exception as e:
        return False, None, str(e)

def create_connection(conn_payload: dict) -> Tuple[bool, Optional[dict]]:
    try:
        r = airbyte_post("/connections", conn_payload)
        if r.status_code in (200, 201):
            return True, r.json()
        return False, {"status_code": r.status_code, "text": r.text}
    except Exception as e:
        return False, {"error": str(e)}

# new helper: trigger a sync job for a connection
def trigger_sync(connection_id: str) -> Tuple[bool, Optional[dict]]:
    """
    Trigger a sync job for the given Airbyte connection id.
    Returns (ok, response_json_or_error_dict).
    Uses the simple payload you provided:
      { "jobType": "sync", "connectionId": "<id>" }
    """
    if not connection_id:
        return False, {"error": "empty connection_id"}
    body = {"jobType": "sync", "connectionId": connection_id}
    try:
        # Try /jobs/create then fallback to /jobs
        r = airbyte_post("/jobs/create", body)
        if r.status_code in (200, 201):
            return True, r.json()
        r2 = airbyte_post("/jobs", body)
        if r2.status_code in (200, 201):
            return True, r2.json()
        return False, {"status_code": r.status_code, "text": r.text, "body_attempted": body}
    except Exception as e:
        return False, {"error": str(e)}


# -----------------------
# Airbyte GET helper + stream handling
# -----------------------
def airbyte_get(path: str, params: dict = None, timeout: int = 30):
    base = AIRBYTE_API_URL.rstrip("/") if AIRBYTE_API_URL else ""
    url = base + path if path.startswith("/") else base + "/" + path
    headers = {"Accept": "application/json"}
    if AIRBYTE_API_TOKEN:
        headers["Authorization"] = f"Bearer {AIRBYTE_API_TOKEN}"
    return requests.get(url, params=params, headers=headers, timeout=timeout)

def list_streams_for_pair(source_id: str, destination_id: str) -> Tuple[bool, Optional[list], Optional[str]]:
    """
    Calls Airbyte public streams endpoint and returns the parsed list (or error).
    """
    if not source_id or not destination_id:
        return False, None, "sourceId and destinationId required"
    try:
        path = "streams"
        params = {"sourceId": source_id, "destinationId": destination_id}
        r = airbyte_get(path, params=params)
        if r.status_code == 200:
            return True, r.json(), None
        return False, None, f"Status {r.status_code}: {r.text}"
    except Exception as e:
        return False, None, str(e)

def choose_stream(streams: list, preferred_name: Optional[str], user_intent: str) -> Optional[dict]:
    """
    Choose a stream dict from the streams list.
    Rules:
      - If preferred_name provided, try exact match (case-insensitive)
      - Else try to match any streamName mentioned in user_intent
      - Else if exactly one stream exists, return it
      - Else return the first stream as a fallback
    """
    if not streams:
        return None
    # normalize
    if preferred_name:
        for s in streams:
            if s.get("streamName", "").lower() == preferred_name.lower():
                return s
    # try to find a stream name mentioned in intent
    lc_intent = (user_intent or "").lower()
    for s in streams:
        name = s.get("streamName", "")
        if name and name.lower() in lc_intent:
            return s
    # if only one stream, return it
    if len(streams) == 1:
        return streams[0]
    # fallback: return first
    return streams[0]

def map_destination_sync_mode(sync_mode_from_list: Optional[str]) -> str:
    """
    Accept a requested sync mode string like 'incremental_append' or 'full_refresh_overwrite'
    and map to Airbyte destinationSyncMode values: 'append', 'overwrite', 'deduped_history'
    Default to 'append'.
    """
    if not sync_mode_from_list:
        return "append"
    m = sync_mode_from_list.lower()
    if "overwrite" in m:
        return "overwrite"
    if "dedup" in m or "deduped" in m:
        return "deduped_history"
    # default -> append
    return "append"

def build_sync_catalog_for_stream(stream_entry: dict, chosen_sync_mode: Optional[str] = None, chosen_cursor_field: Optional[list] = None, chosen_primary_key: Optional[list] = None) -> dict:
    """
    Construct Airbyte syncCatalog for a single stream entry returned by /streams.
    stream_entry is expected to contain keys like streamName, streamnamespace, defaultCursorField, sourceDefinedPrimaryKey, propertyFields.
    """
    if not stream_entry:
        return {"streams": []}

    stream_name = stream_entry.get("streamName")
    namespace = stream_entry.get("streamnamespace") or None

    # determine cursor field and primary key defaults from stream metadata
    default_cursor = stream_entry.get("defaultCursorField") or []
    source_pk = stream_entry.get("sourceDefinedPrimaryKey") or []
    # propertyFields is an array-of-arrays of property names; not used directly for config here
    # If user provided overrides, use them
    cursor_field = chosen_cursor_field if chosen_cursor_field is not None else default_cursor
    primary_key = chosen_primary_key if chosen_primary_key is not None else source_pk

    # sync mode selection: prefer user choice if given; otherwise try to pick a sensible default
    # If chosen_sync_mode is None, prefer incremental if defaultCursorField present, else pick first full_refresh variant
    sync_mode = chosen_sync_mode
    if not sync_mode:
        # pick a sensible sync mode present in stream_entry.syncModes if available
        modes = stream_entry.get("syncModes") or []
        # prefer an incremental mode if cursor exists
        if cursor_field and any(m.startswith("incremental") for m in modes):
            # pick the first incremental
            found = next((m for m in modes if m.startswith("incremental")), None)
            sync_mode = found or (modes[0] if modes else "incremental_append")
        else:
            # fall back to a full_refresh variant if present
            found = next((m for m in modes if m.startswith("full_refresh")), None)
            sync_mode = found or (modes[0] if modes else "full_refresh_append")

    destination_sync_mode = map_destination_sync_mode(sync_mode)

    # build the minimal stream json schema placeholder (Airbyte accepts an empty schema in many versions;
    # for robustness include an empty object)
    stream_obj = {
        "stream": {
            "name": stream_name,
            "json_schema": {},  # ideally populated from discovery; kept empty as a placeholder
            **({"namespace": namespace} if namespace else {})
        },
        "config": {
            "sync_mode": sync_mode,
            "destination_sync_mode": destination_sync_mode,
            "cursor_field": cursor_field or [],
            "primary_key": primary_key or []
        }
    }

    return {"streams": [stream_obj]}

# -----------------------
# Main generation + optional validation
# -----------------------
def generate_connection_from_intent(user_intent: str, k: int = RAG_TOP_K, max_attempts: int = MAX_REPAIR_ATTEMPTS, force_validate: Optional[bool] = None, auto_create: Optional[bool] = None, run_sync: bool = False):
    """
    force_validate:
       - True: require Airbyte validation (error if unreachable)
       - False: skip validation entirely
       - None: auto-detect based on AIRBYTE_API_URL and reachability
    auto_create:
       - True: call /connections/create when validated
       - False: don't create
       - None: use CREATE_IN_AIRBYTE_ENV
    run_sync:
       - True: after successful create, attempt to trigger a sync job for the created connection
    """
    if auto_create is None:
        auto_create = CREATE_IN_AIRBYTE_ENV
    emb, vs = get_embedding_and_chroma()
    # retrieve top-k
    results = vs.similarity_search_with_score(user_intent, k=k)
    retrieved = [(getattr(doc, "page_content", ""), getattr(doc, "metadata", {}), float(score)) for doc, score in results]
    prompt = build_prompt(retrieved, user_intent)
    llm = get_chat_llm(BEDROCK_LLM_ARN)

    # decide validation mode (initial)
    if force_validate is True:
        # we'll still override later if bundle lacks concrete details, per user's request
        validate = True
    elif force_validate is False:
        validate = False
    else:
        # auto-detect based on AIRBYTE_API_URL reachability
        validate = bool(AIRBYTE_API_URL and is_airbyte_reachable(AIRBYTE_API_URL))
        if validate:
            log.info("Airbyte detected and reachable at %s — validation ENABLED", AIRBYTE_API_URL)
        else:
            log.info("Airbyte not detected or unreachable — running in GENERATE-ONLY mode")

    # helper: determine if generated source/destination contains enough concrete data to attempt API calls
    def has_sufficient_details_for_validation(entity: dict) -> bool:
        """
        Returns True when entity looks like it has concrete fields that would make
        validating/creating it in Airbyte meaningful. We consider:
         - any definition id that is not a placeholder (i.e. does not start with '<<')
         - or connection config containing a host/database/bucket/file/connectionString non-placeholder
        """
        if not isinstance(entity, dict):
            return False
        # check for various definition id keys
        defid = entity.get("definitionId")
        if defid and isinstance(defid, str) and defid.strip() and not defid.strip().startswith("<<"):
            return True
        # inspect configuration variants
        cfg = entity.get("configuration") or {}
        if not isinstance(cfg, dict):
            return False
        # keys that usually indicate a concrete connector config
        signature_keys = ["host", "database", "schema", "bucket", "account", "connectionString", "url"]
        for k in signature_keys:
            v = cfg.get(k)
            if isinstance(v, str) and v.strip() and not v.strip().startswith("<<"):
                return True
        # also check for credentials present (username/password) if they're concrete (not placeholders)
        user = cfg.get("username") or cfg.get("user")
        pwd = cfg.get("password") or cfg.get("pwd")
        if user and isinstance(user, str) and user.strip() and not user.strip().startswith("<<"):
            if pwd and isinstance(pwd, str) and pwd.strip() and not pwd.strip().startswith("<<"):
                return True
        return False

    attempt = 0
    last_error = None
    while attempt < max_attempts:
        attempt += 1
        log.info("LLM generation attempt %d/%d", attempt, max_attempts)
        try:
            resp = llm.invoke(prompt)
            llm_text = getattr(resp, "content", None) or getattr(resp, "text", None) or str(resp)
        except Exception as e:
            last_error = f"LLM call failed: {e}"
            log.exception(last_error)
            time.sleep(1 + attempt)
            continue

        conn_bundle = extract_json_from_text(llm_text)
        if not conn_bundle:
            last_error = f"LLM output not parseable as JSON. Output excerpt:\n{llm_text[:1000]!s}"
            log.warning(last_error)
            prompt += "\n\nNOTE: Your previous output was not valid JSON. Return ONLY the JSON object called connection_bundle."
            continue

        # Basic key validation
        req = {"connection_name", "source", "destination", "sync"}
        if not req.issubset(set(conn_bundle.keys())):
            missing = req - set(conn_bundle.keys())
            last_error = f"Missing required keys: {missing}"
            log.warning(last_error)
            prompt += f"\n\nYour JSON is missing required keys: {missing}. Return corrected JSON only."
            continue

        # *** New behavior: if generated source/destination lack concrete details, skip validation ***
        src_entity = conn_bundle.get("source", {}) or {}
        dst_entity = conn_bundle.get("destination", {}) or {}
        if validate:
            src_has = has_sufficient_details_for_validation(src_entity)
            dst_has = has_sufficient_details_for_validation(dst_entity)
            if not (src_has and dst_has):
                # If either side lacks details, we will not attempt Airbyte validation/create.
                log.info("Insufficient concrete details detected for validation (src_has=%s dst_has=%s). Skipping Airbyte API validation and returning generated bundle.", src_has, dst_has)
                # Optionally, you can still fill workspaceId placeholders here:
                if isinstance(src_entity, dict) and not src_entity.get("workspaceId"):
                    src_entity["workspaceId"] = WORKSPACE_ID
                    conn_bundle["source"] = src_entity
                if isinstance(dst_entity, dict) and not dst_entity.get("workspaceId"):
                    dst_entity["workspaceId"] = WORKSPACE_ID
                    conn_bundle["destination"] = dst_entity
                return conn_bundle, None

        # If validation is disabled (explicitly), return the bundle immediately
        if not validate:
            log.info("Validation disabled — returning generated connection bundle (no API checks).")
            return conn_bundle, None

        # validation enabled and sufficient details present: attempt to create/check sources/destinations and connection
        src = conn_bundle["source"].copy()
        dst = conn_bundle["destination"].copy()
        src_payload = {
            "name": src.get("name", conn_bundle["connection_name"] + "_src"),
            "definitionId": src.get("definitionId", "<<SOURCE_DEFINITION_ID>>"),
            "workspaceId": src.get("workspaceId", WORKSPACE_ID),
            "configuration": src.get("configuration", src.get("connectionConfiguration", {}))
        }
        dst_payload = {
            "name": dst.get("name", conn_bundle["connection_name"] + "_dst"),
            "definitionId": dst.get("definitionId", "<<DEST_DEFINITION_ID>>"),
            "workspaceId": dst.get("workspaceId", WORKSPACE_ID),
            "configuration": dst.get("configuration", dst.get("connectionConfiguration", {}))
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

        # Build a simplified connection check payload (production should build syncCatalog via discover_schema)
        # -----------------------
        # Build strict-format connection payload (user requested exact schema)
        # Format required:
        # {
        #   "configurations": { "streams": [ { "name": "customer" } ] },
        #   "sourceId": "<id>",
        #   "name": "<connection_name>",
        #   "destinationId": "<id>"
        # }
        # -----------------------

        # Start base payload
        conn_payload = {
            "name": conn_bundle.get("connection_name"),
            "sourceId": src_id,
            "destinationId": dst_id,
        }

        def normalize_requested_list(val):
            if not val:
                return []
            if isinstance(val, list):
                return [str(x).strip() for x in val if x]
            if isinstance(val, str):
                return [s.strip() for s in val.split(",") if s.strip()]
            return []

        # Gather explicit requests from metadata
        meta = conn_bundle.get("metadata", {}) or {}
        requested_streams = normalize_requested_list(
            meta.get("requested_streams") or meta.get("requested_stream") or meta.get("stream") or meta.get(
                "stream_name")
        )
        if requested_streams:
            log.info("Requested streams from metadata: %s", requested_streams)

        # Fetch available streams from Airbyte
        ok_streams, streams_list, streams_err = list_streams_for_pair(src_id, dst_id)

        selected_names = []

        if ok_streams and isinstance(streams_list, list) and len(streams_list) > 0:
            # Build ordered list of available stream names
            available = []
            for s in streams_list:
                nm = s.get("streamName") or s.get("name") or ""
                if nm:
                    available.append(nm)

            log.info("Available streams from Airbyte: %s", available)

            # 1) If explicit requested list present, include those that match available names (case-insensitive)
            if requested_streams:
                for req in requested_streams:
                    # try exact CI match first
                    match = next((a for a in available if a.lower() == req.lower()), None)
                    if not match:
                        # try substring CI match
                        match = next((a for a in available if req.lower() in a.lower()), None)
                    if match:
                        selected_names.append(match)
                    else:
                        log.warning("Requested stream '%s' not found in available streams; skipping.", req)

            else:
                # 2) No explicit request: detect all stream names mentioned in user_intent
                lc_intent = (user_intent or "").lower()
                for a in available:
                    a_variants = set()
                    a_variants.add(a.lower())
                    # replace underscores/hyphens with spaces (common in table names)
                    a_variants.add(a.lower().replace("_", " ").replace("-", " "))
                    # also try singular/plural naive variant
                    if a.endswith("s"):
                        a_variants.add(a[:-1].lower())
                    else:
                        a_variants.add(a + "s")
                    # if any variant appears in intent, select it
                    if any(v in lc_intent for v in a_variants):
                        selected_names.append(a)

            # dedupe preserving order
            seen = set()
            selected_names = [x for x in selected_names if not (x in seen or seen.add(x))]

            # 3) Fallback special-case: if intent mentions common pair 'customer' and 'commits', ensure both included
            if not selected_names:
                lc_intent = (user_intent or "").lower()
                for special in ("customer", "commits"):
                    if special in lc_intent:
                        match = next((a for a in available if a.lower() == special), None)
                        if match and match not in selected_names:
                            selected_names.append(match)

            # 4) Final fallback: if still empty, include first stream to keep payload valid
            if not selected_names and available:
                selected_names.append(available[0])

        else:
            log.warning("Could not list streams for source/destination: %s", streams_err)
            # If we couldn't query Airbyte but user explicitly requested streams, use those as-is
            if requested_streams:
                selected_names = requested_streams[:]
            else:
                selected_names = ["<<STREAM_NAME>>"]

        log.info("Selected streams for connection: %s", selected_names)

        # Build configurations.streams array
        conn_payload["configurations"] = {"streams": [{"name": name} for name in selected_names]}

        # Now create the connection
        created_ok, created_resp = create_connection(conn_payload)

        if created_ok:
            log.info("Connection created in Airbyte: %s", created_resp)
            # try to extract connection id from created_resp
            conn_id = None
            if isinstance(created_resp, dict):
                conn_id = created_resp.get("connectionId") or created_resp.get("id")
                if not conn_id:
                    if "connection" in created_resp and isinstance(created_resp["connection"], dict):
                        conn_id = created_resp["connection"].get("connectionId") or created_resp["connection"].get("id")
                if not conn_id:
                    for k in ("item", "resource"):
                        v = created_resp.get(k)
                        if isinstance(v, dict):
                            conn_id = v.get("connectionId") or v.get("id")
                            if conn_id:
                                break

            # If run-sync requested, trigger it
            if run_sync:
                ok_job, job_resp = trigger_sync(conn_id)
                if ok_job:
                    log.info("Triggered sync job for connection %s: %s", conn_id, job_resp)
                else:
                    log.warning("Failed to trigger sync job for connection %s: %s", conn_id, job_resp)
                return conn_bundle, {"created": created_resp, "job": job_resp}
            else:
                return conn_bundle, created_resp
        else:
            last_error = f"Connection create failed: {created_resp}"
            log.warning(last_error)
            prompt += f"\n\nAirbyte connections/create returned error:\n{created_resp}\nPlease return corrected connection_bundle JSON only."
            continue

    # if we exhaust attempts
    log.error("Failed to generate a validated connection bundle after %d attempts. Last error: %s", max_attempts, last_error)
    return None, {"error": last_error}


# -----------------------
# CLI
# -----------------------
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Generate Airbyte connection bundle (RAG + Bedrock ARN LLM).")
    parser.add_argument("--query", "-q", required=True, help="Natural language intent")
    parser.add_argument("--k", type=int, default=RAG_TOP_K, help="Top-k retrieval")
    parser.add_argument("--attempts", type=int, default=MAX_REPAIR_ATTEMPTS, help="Max repair attempts")
    parser.add_argument("--create", action="store_true", help="Also call /connections/create when validated")
    parser.add_argument("--no-validate", action="store_true", help="Force skip Airbyte validation even if AIRBYTE_API_URL is set")
    parser.add_argument("--validate", action="store_true", help="Force validation (error if Airbyte unreachable)")
    parser.add_argument("--run-sync", "-s", action="store_true", help="Trigger a sync job for the created connection")
    args = parser.parse_args()

    # decide auto_create/validate flags
    auto_create = args.create if args.create else None  # None means use env default
    if args.no_validate:
        force_validate = False
    elif args.validate:
        force_validate = True
    else:
        force_validate = None

    run_sync_flag = args.run_sync
    bundle, created = generate_connection_from_intent(args.query, k=args.k, max_attempts=args.attempts, force_validate=force_validate, auto_create=auto_create, run_sync=run_sync_flag)
    if bundle:
        print("\n=== Final connection bundle ===")
        print(json.dumps(bundle, indent=2))
        if created:
            print("\n=== Airbyte create/response ===")
            print(json.dumps(created, indent=2))
    else:
        print("\nFailed to generate validated connection bundle. See logs for details.")
