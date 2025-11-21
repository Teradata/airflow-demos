#!/usr/bin/env python3
"""
index_manager.py

Unified index manager for ACA Retriever:
- incremental indexing (sha256 manifest)
- full rebuild into versioned directories + atomic swap
- deterministic chunk ids for safe upserts
- delete removed docs from index
- smoke tests and embedding-dimension checks
- locking to prevent concurrent runs

"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from dotenv import load_dotenv

# Locking (unix-only file lock via fcntl). On Windows fallback to simple lock file check.
try:
    import fcntl

    _HAS_FCNTL = True
except Exception:
    _HAS_FCNTL = False

# LangChain / Chroma / Bedrock imports (LangChain v0.2+ split packages)
from langchain_core.documents import Document
from langchain_community.document_loaders import JSONLoader, TextLoader, UnstructuredFileLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_aws import BedrockEmbeddings
from langchain_chroma import Chroma

# Logging config
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("index_manager")

load_dotenv()


# -------------------------
# Helpers
# -------------------------
def _is_primitive(v) -> bool:
    return isinstance(v, (str, int, float, bool)) or v is None


def sanitize_metadata_value(v):
    """
    Convert metadata values into Chroma-acceptable primitives.
    - primitives unchanged
    - dict/list -> JSON string
    - other objects -> str()
    """
    if _is_primitive(v):
        return v
    try:
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        # fall back to str for anything else
        return str(v)
    except Exception:
        return str(v)


def sanitize_metadata(md: dict) -> dict:
    """
    Return a new metadata dict with only primitive values (or JSON-serialized strings).
    Keeps keys as-is but ensures values are str/int/float/bool/None.
    """
    if not isinstance(md, dict):
        return {}
    out: Dict[str, object] = {}
    for k, v in md.items():
        try:
            out[k] = sanitize_metadata_value(v)
        except Exception:
            out[k] = str(v)
    return out


def compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def now_tag() -> str:
    return datetime.utcnow().strftime("v%Y%m%dT%H%M%SZ")


def read_manifest(manifest_path: Path) -> Dict:
    if not manifest_path.exists():
        return {}
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read manifest; starting fresh.")
        return {}


def write_manifest(manifest_path: Path, obj: Dict):
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


def list_files(root: Path, exts=None) -> List[Path]:
    if exts is None:
        exts = {".md", ".txt", ".json", ".pdf", ".rst", ".yaml", ".yml"}
    files = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in exts]
    return files


def load_file_via_loader(path: Path) -> List[Document]:
    """
    Load a path into one or more langchain Document objects.

    Behavior:
    - Text files (.md/.txt/.rst): use TextLoader
    - JSON files: prefer to extract top-level connectionSpecification (index that as a single doc),
                 otherwise fallback to JSONLoader
    - YAML files (.yaml/.yml): attempt yaml.safe_load(); if parsed object contains a top-level
                 'connectionSpecification' or a 'data' block that looks like a connector manifest,
                 index that sub-object as a single Document (page_content = serialized JSON).
                 Otherwise serialize the whole YAML object to JSON and index as a single Document.
    - Other types: UnstructuredFileLoader fallback
    - On any loader/parse error, fallback to raw text content.
    """
    suffix = path.suffix.lower()
    docs: List[Document] = []
    try:
        if suffix in {".md", ".txt", ".rst"}:
            loader = TextLoader(str(path), encoding="utf-8")
            docs = loader.load()
        elif suffix == ".json":
            # Prefer to load JSON and, if it contains a top-level connectionSpecification,
            # index that spec as one JSON string doc so retrieval returns the full spec.
            try:
                raw = path.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                # If it's a connector spec with connectionSpecification, index that block
                if isinstance(parsed, dict) and "connectionSpecification" in parsed:
                    spec = parsed["connectionSpecification"]
                    docs = [
                        Document(
                            page_content=json.dumps(spec, indent=2),
                            metadata={"source": str(path), "spec_json": json.dumps(spec, ensure_ascii=False)},
                        )
                    ]
                else:
                    loader = JSONLoader(str(path))
                    docs = loader.load()
            except Exception as ex_json:
                log.warning("JSON handling fallback for %s: %s — using raw text", path, ex_json)
                text = path.read_text(encoding="utf-8", errors="ignore")
                docs = [Document(page_content=text, metadata={"source": str(path)})] if text else []
        elif suffix in {".yaml", ".yml"}:
            # YAML handling: prefer connector-style blocks (connectionSpecification or top-level data)
            try:
                raw = path.read_text(encoding="utf-8")
                parsed = yaml.safe_load(raw)
                if parsed is None:
                    # empty yaml -> no docs
                    docs = []
                elif isinstance(parsed, dict):
                    # If YAML contains connectionSpecification (Airbyte spec), index that
                    if "connectionSpecification" in parsed and isinstance(parsed["connectionSpecification"], (dict, list)):
                        spec = parsed["connectionSpecification"]
                        docs = [
                            Document(
                                page_content=json.dumps(spec, indent=2),
                                metadata={"source": str(path), "spec_json": spec},
                            )
                        ]
                    # If YAML is a connector manifest (top-level 'data' block), index 'data' itself
                    elif "data" in parsed and isinstance(parsed["data"], dict):
                        data_block = parsed["data"]
                        docs = [
                            Document(
                                page_content=json.dumps(data_block, indent=2),
                                metadata={"source": str(path), "manifest": True, "manifest_json": data_block},
                            )
                        ]
                    else:
                        # Fallback: index entire YAML as a single JSON string doc
                        docs = [Document(page_content=json.dumps(parsed, indent=2), metadata={"source": str(path)})]
                else:
                    # scalar or list -> serialize and index
                    docs = [Document(page_content=json.dumps(parsed, indent=2), metadata={"source": str(path)})]
            except Exception as ex_yaml:
                log.warning("YAML parse failed for %s: %s — falling back to raw text", path, ex_yaml)
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                    docs = [Document(page_content=text, metadata={"source": str(path)})] if text else []
                except Exception:
                    docs = []
        else:
            loader = UnstructuredFileLoader(str(path))
            docs = loader.load()
    except Exception as ex:
        log.warning("Loader failed for %s: %s — falling back to raw text", path, ex)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
            docs = [Document(page_content=text, metadata={"source": str(path)})] if text else []
        except Exception:
            docs = []

    # Ensure source metadata present
    for d in docs:
        if "source" not in d.metadata:
            d.metadata["source"] = str(path)
    return docs


def chunk_documents(docs: List[Document], chunk_size: int, overlap: int) -> List[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=chunk_size, chunk_overlap=overlap)
    return splitter.split_documents(docs)


def deterministic_chunk_id(file_path: str, file_sha: str, chunk_index: int) -> str:
    # normalize path to POSIX and make ids cross-platform deterministic
    normalized = Path(file_path).as_posix().replace("/", "::")
    return f"{normalized}::{file_sha}::chunk{chunk_index}"


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


# Simple retry helper for embedding calls
def retry_embed(func, *args, retries: int = 3, backoff_base: float = 0.5, **kwargs):
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt == retries:
                log.exception("Embedding failed after %d attempts", attempt)
                raise
            wait = backoff_base * (2 ** (attempt - 1)) * (1 + random.random() * 0.1)
            log.warning("Embed attempt %d failed: %s - retrying in %.2fs", attempt, e, wait)
            time.sleep(wait)
    # should never reach here
    raise last_exc


# -------------------------
# IndexManager
# -------------------------
class IndexManager:
    def __init__(
        self,
        docs_dir: str,
        persist_root: str,
        manifests_dir: str = "manifests",
        region: str = "us-west-2",
        model_id: str = "amazon.titan-embed-text-v2:0",
        chunk_size: int = 700,
        chunk_overlap: int = 70,
        keep_versions: int = 3,
        lock_file: Optional[str] = None,
    ):
        self.docs_dir = Path(docs_dir)
        self.persist_root = Path(persist_root)
        self.manifests_dir = Path(manifests_dir)
        self.region = region
        self.model_id = model_id
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.keep_versions = keep_versions
        self.lock_file = lock_file or str(self.manifests_dir / ".index_lock")
        self.manifest_path = self.manifests_dir / "index_manifest.json"
        self.embedding: Optional[BedrockEmbeddings] = None
        ensure_dir(self.manifests_dir)
        ensure_dir(self.persist_root)
        self.lock_fd = None

    def _acquire_lock(self):
        # open a lock file descriptor so we can use fcntl on unix
        try:
            self.lock_fd = open(self.lock_file, "w")
        except Exception:
            # fallback: ensure lock_file path exists (will attempt writes later)
            self.lock_fd = None

        if _HAS_FCNTL and self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                log.debug("Acquired fcntl lock")
            except IOError:
                log.error("Another indexing process is running (lock held). Exiting.")
                sys.exit(2)
        else:
            try:
                # If a recent lock file exists, assume another process is running
                if os.path.exists(self.lock_file) and (time.time() - os.path.getmtime(self.lock_file) < 3600):
                    log.error("Another indexing process is running (lock file exists). Exiting.")
                    sys.exit(2)
                # touch the lock file
                Path(self.lock_file).write_text(str(time.time()))
            except Exception:
                # if we cannot create a lock file, keep going but log
                log.warning("Unable to create lock file; proceeding without robust locking.")

    def _release_lock(self):
        try:
            if _HAS_FCNTL and self.lock_fd is not None:
                try:
                    fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                except Exception:
                    pass
            if self.lock_fd:
                try:
                    self.lock_fd.close()
                except Exception:
                    pass
            try:
                if os.path.exists(self.lock_file):
                    os.remove(self.lock_file)
            except Exception:
                pass
        except Exception:
            pass

    def _init_embedding(self):
        if self.embedding is None:
            log.info("Initializing Bedrock embeddings (model=%s region=%s)", self.model_id, self.region)
            self.embedding = BedrockEmbeddings(region_name=self.region, model_id=self.model_id)
            try:
                sample = retry_embed(self.embedding.embed_documents, ["__dim_check__"], retries=3)
                dim = len(sample[0])
                log.info("Embedding dimension: %d", dim)
            except Exception as e:
                log.exception("Failed to call embedding model; check AWS credentials/permissions: %s", e)
                raise

    def _open_collection(self, persist_dir: Path, collection_name: str = "airbyte_connectors"):
        # pass embedding_function explicitly for consistency where possible
        if self.embedding is not None:
            vs = Chroma(persist_directory=str(persist_dir), collection_name=collection_name, embedding_function=self.embedding)
        else:
            vs = Chroma(persist_directory=str(persist_dir), collection_name=collection_name)
        return vs, vs._collection

    def incremental_index(self):
        self._acquire_lock()
        try:
            manifest = read_manifest(self.manifest_path)
            prev_files = manifest.get("files", {})
            files = list_files(self.docs_dir)
            current: Dict[str, Dict] = {}
            for p in files:
                sha = compute_sha256(p)
                current[str(p)] = {"sha": sha, "path": str(p)}

            added = [p for p in current if p not in prev_files]
            changed = [p for p in current if p in prev_files and current[p]["sha"] != prev_files[p]["sha"]]
            deleted = [p for p in prev_files if p not in current]

            log.info(
                "Files discovered: %d, added: %d, changed: %d, deleted: %d",
                len(files),
                len(added),
                len(changed),
                len(deleted),
            )

            if not (added or changed or deleted):
                log.info("No changes detected; nothing to do.")
                return

            self._init_embedding()

            current_dir = self.persist_root / "current"
            if not current_dir.exists():
                vtag = now_tag()
                current_dir = self.persist_root / vtag
                log.info("No current index found; creating initial index at: %s", current_dir)
                ensure_dir(current_dir)
                vs, _ = self._open_collection(current_dir)
                # create an initial manifest (dimension check)
                dim = len(retry_embed(self.embedding.embed_documents, ["__dim_check__"], retries=3)[0])
                manifest = {
                    "files": {},
                    "embedding_model_id": self.model_id,
                    "dimension": dim,
                    "index_version": vtag,
                }
                write_manifest(self.manifest_path, manifest)

            vs, collection = self._open_collection(current_dir)

            to_index = added + changed
            if to_index:
                log.info("Indexing %d files", len(to_index))
                for pstr in to_index:
                    p = Path(pstr)
                    docs = load_file_via_loader(p)
                    if not docs:
                        log.warning("No docs loaded for %s, skipping", p)
                        continue
                    chunks = chunk_documents(docs, chunk_size=self.chunk_size, overlap=self.chunk_overlap)
                    texts = [c.page_content for c in chunks]
                    metadatas = []
                    ids = []
                    file_sha = current[pstr]["sha"]
                    for i, c in enumerate(chunks):
                        ids.append(deterministic_chunk_id(pstr, file_sha, i))
                        raw_md = dict(c.metadata or {})
                        raw_md.update({"source": pstr, "file_sha": file_sha, "chunk_index": i})
                        metadatas.append(sanitize_metadata(raw_md))
                    try:
                        embeddings = retry_embed(self.embedding.embed_documents, texts, retries=3)
                    except Exception as e:
                        log.exception("Embedding failed for %s: %s", p, e)
                        raise
                    # upsert using low-level collection API
                    collection.upsert(ids=ids, embeddings=embeddings, metadatas=metadatas, documents=texts)
                    manifest.setdefault("files", {})[pstr] = {
                        "sha": file_sha,
                        "last_indexed": datetime.utcnow().isoformat(),
                        "chunks": len(chunks),
                    }
                    write_manifest(self.manifest_path, manifest)
                    log.info("Upserted %d chunks for %s", len(chunks), pstr)

            if deleted:
                log.info("Deleting index entries for %d removed files", len(deleted))
                delete_ids: List[str] = []
                for pstr in deleted:
                    prev = prev_files.get(pstr)
                    if not prev:
                        continue
                    prev_sha = prev.get("sha")
                    prev_chunks = int(prev.get("chunks", 0))
                    for i in range(prev_chunks):
                        delete_ids.append(deterministic_chunk_id(pstr, prev_sha, i))
                if delete_ids:
                    try:
                        collection.delete(ids=delete_ids)
                        log.info("Deleted %d chunk ids from collection", len(delete_ids))
                    except Exception:
                        log.exception("Failed to delete ids; continuing")
                for pstr in deleted:
                    manifest["files"].pop(pstr, None)
                write_manifest(self.manifest_path, manifest)

            log.info("Incremental indexing complete.")
        finally:
            self._release_lock()

    def full_rebuild(self, smoke_queries: Optional[List[str]] = None):
        self._acquire_lock()
        try:
            vtag = now_tag()
            new_dir = self.persist_root / vtag
            if new_dir.exists():
                log.info("Removing pre-existing new_dir: %s", new_dir)
                shutil.rmtree(new_dir)
            ensure_dir(new_dir)
            log.info("Starting full rebuild into %s", new_dir)

            self._init_embedding()

            files = list_files(self.docs_dir)
            all_items = []  # list of tuples (p, file_sha, chunks)
            dim = len(retry_embed(self.embedding.embed_documents, ["__dim_check__"], retries=3)[0])
            manifest = {
                "files": {},
                "embedding_model_id": self.model_id,
                "dimension": dim,
                "index_version": vtag,
            }

            for p in files:
                docs = load_file_via_loader(p)
                if not docs:
                    continue
                chunks = chunk_documents(docs, chunk_size=self.chunk_size, overlap=self.chunk_overlap)
                file_sha = compute_sha256(p)
                for i, c in enumerate(chunks):
                    md = dict(c.metadata or {})
                    md.update({"source": str(p), "file_sha": file_sha, "chunk_index": i})
                    c.metadata = md
                all_items.append((p, file_sha, chunks))
                manifest["files"][str(p)] = {
                    "sha": file_sha,
                    "last_indexed": datetime.utcnow().isoformat(),
                    "chunks": len(chunks),
                }

            # prepare flattened lists
            texts: List[str] = []
            metadatas: List[Dict] = []
            ids: List[str] = []
            for p, file_sha, chunks in all_items:
                for i, c in enumerate(chunks):
                    texts.append(c.page_content)
                    # ensure metadata exists
                    md = dict(c.metadata or {})
                    md.update({"source": str(p), "file_sha": file_sha, "chunk_index": i})
                    metadatas.append(sanitize_metadata(md))
                    ids.append(deterministic_chunk_id(str(p), file_sha, i))

            # Guard: nothing to index
            if not texts:
                log.warning("No texts to index in full_rebuild; removing new_dir and aborting.")
                shutil.rmtree(new_dir, ignore_errors=True)
                return

            log.info("Total chunks to index: %d", len(texts))

            # batch embeddings
            EMB_BATCH = 64
            embeddings_all: List[List[float]] = []
            for i in range(0, len(texts), EMB_BATCH):
                batch = texts[i : i + EMB_BATCH]
                log.info("Embedding batch %d..%d", i, i + len(batch))
                embeddings_all.extend(retry_embed(self.embedding.embed_documents, batch, retries=3))

            # create empty collection in new_dir (do NOT call from_documents with empty list)
            vs_new = Chroma(persist_directory=str(new_dir), collection_name="airbyte_connectors", embedding_function=self.embedding)
            collection = vs_new._collection

            # upsert in batches
            for i in range(0, len(texts), EMB_BATCH):
                b_ids = ids[i : i + EMB_BATCH]
                b_emb = embeddings_all[i : i + EMB_BATCH]
                b_mds = metadatas[i : i + EMB_BATCH]
                b_docs = texts[i : i + EMB_BATCH]
                if not b_ids:
                    continue
                collection.upsert(ids=b_ids, embeddings=b_emb, metadatas=b_mds, documents=b_docs)
                log.info("Upserted batch %d..%d", i, i + len(b_ids))

            # write manifest for new index
            write_manifest(self.manifests_dir / f"manifest_{vtag}.json", manifest)
            log.info("New index built. Running smoke tests (if provided)")

            if smoke_queries:
                ok = self._run_smoke_tests_on_dir(new_dir, smoke_queries)
                if not ok:
                    log.error("Smoke tests failed. Aborting swap and removing new index directory.")
                    shutil.rmtree(new_dir)
                    return

            # atomic swap
            current_link = self.persist_root / "current"
            tmp_link = self.persist_root / f"tmp_{vtag}"
            if tmp_link.exists():
                tmp_link.unlink()
            os.symlink(str(new_dir.resolve()), str(tmp_link))
            tmp_link.replace(current_link)
            global_manifest = {
                "files": manifest["files"],
                "embedding_model_id": self.model_id,
                "dimension": manifest["dimension"],
                "index_version": vtag,
            }
            write_manifest(self.manifest_path, global_manifest)
            log.info("Atomic swap complete; current now -> %s", new_dir)

            # retention
            self._prune_old_versions()
        finally:
            self._release_lock()

    def _prune_old_versions(self):
        dirs = [p for p in self.persist_root.iterdir() if p.is_dir() and p.name.startswith("v")]
        dirs_sorted = sorted(dirs, key=lambda p: p.name, reverse=True)
        to_remove = dirs_sorted[self.keep_versions :]
        for d in to_remove:
            try:
                shutil.rmtree(d)
                log.info("Pruned old index dir: %s", d)
            except Exception:
                log.exception("Failed to remove old index dir: %s", d)

    def _run_smoke_tests_on_dir(self, dir_path: Path, smoke_queries: List[str]) -> bool:
        try:
            # IMPORTANT: pass the same embedding_function used to build the index
            vs = Chroma(persist_directory=str(dir_path), collection_name="airbyte_connectors", embedding_function=self.embedding)

            # verify embedding dim using the actual embedding function (retry-safe)
            emb_dim = len(retry_embed(self.embedding.embed_documents, ["__dim_check__"], retries=3)[0])
            log.info("Smoke: embedding dim reported as %d", emb_dim)

            # for each golden query, use the vector-search that relies on the same embedding function
            for q in smoke_queries:
                try:
                    res = vs.similarity_search_with_score(q, k=1)
                    if not res:
                        log.warning("Smoke: query '%s' returned no results", q)
                        return False
                except Exception:
                    log.exception("Smoke query failed for: %s", q)
                    return False
            return True
        except Exception:
            log.exception("Smoke tests encountered an error")
            return False


# -------------------------
# CLI
# -------------------------
def parse_args():
    p = argparse.ArgumentParser(description="ACA Index Manager")
    p.add_argument("--docs-dir", default="./airbyte_docs", help="Directory with source docs to index")
    p.add_argument("--persist-root", default="./chroma_store", help="Root directory to persist chroma indexes")
    p.add_argument("--manifests-dir", default="./manifests", help="Manifest directory")
    p.add_argument("--region", default=os.getenv("AWS_REGION", "us-west-2"), help="AWS region for Bedrock")
    p.add_argument(
        "--model-id",
        default=os.getenv("BEDROCK_EMBED_MODEL", "amazon.titan-embed-text-v2:0"),
        help="Bedrock embed model id",
    )
    p.add_argument("--chunk-size", type=int, default=700)
    p.add_argument("--chunk-overlap", type=int, default=70)
    p.add_argument("--keep-versions", type=int, default=3)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--incremental", action="store_true", help="Run incremental index pass")
    grp.add_argument("--rebuild", action="store_true", help="Run full rebuild and atomic swap")
    p.add_argument("--smoke-query", action="append", help="Add a smoke query (can be passed multiple times)")
    return p.parse_args()


def main():
    args = parse_args()
    mgr = IndexManager(
        docs_dir=args.docs_dir,
        persist_root=args.persist_root,
        manifests_dir=args.manifests_dir,
        region=args.region,
        model_id=args.model_id,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        keep_versions=args.keep_versions,
    )

    if args.incremental:
        mgr.incremental_index()
    elif args.rebuild:
        smoke_queries = args.smoke_query or []
        mgr.full_rebuild(smoke_queries=smoke_queries)
    else:
        log.error("No action chosen")


if __name__ == "__main__":
    main()
