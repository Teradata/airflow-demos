
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Dict, List, Optional

# LangChain Bedrock bindings (assumed installed in the environment)

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("util")


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


# helper to create safe filenames
def _safe_filename(name: str) -> str:
    keep = ("abcdefghijklmnopqrstuvwxyz0123456789-_")
    s = name.lower().replace(" ", "_")
    return "".join((c for c in s if c in keep))[:200]





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