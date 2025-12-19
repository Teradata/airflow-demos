
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

from prompt import PROMPT_TEMPLATE
from util import _safe_filename, extract_json_from_text

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("llm")

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

if not BEDROCK_LLM_ARN:
    log.error("Environment variable BEDROCK_LLM_ARN must be set to your Bedrock inference profile ARN.")
    raise SystemExit(1)


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