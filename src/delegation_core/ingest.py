"""
ingest.py : External folder ingestion (ABNER).

Index files from any path without moving or modifying them.
Uses embeddings.chunk_text for long documents and persists an ingestion registry
so re-runs are safe (upsert semantics, no duplicates).

New in v0.2.
"""

import fnmatch
import json
import logging
import os
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from .config import CONFIG_DIR
from .embeddings import chunk_text
from .vault import client_from_path

logger = logging.getLogger("ingest")

_REGISTRY_FILE = CONFIG_DIR / "ingested_sources.json"

#: Extensions that mean "this was a codebase, not a document folder": used only
#: to phrase the hint, not to decide what gets indexed.
_CODE_HINT_EXTS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".lua", ".zig", ".ex", ".exs", ".dart", ".vue", ".svelte", ".sh", ".sql",
})


def _load_registry() -> dict:
    try:
        return json.loads(_REGISTRY_FILE.read_text(encoding="utf-8")) if _REGISTRY_FILE.exists() else {}
    except Exception:
        return {}


def _atomic_write_registry(registry: dict):
    target_dir = _REGISTRY_FILE.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=str(target_dir), delete=False, encoding="utf-8") as tf:
            json.dump(registry, tf, indent=2)
            tmp = tf.name
        os.replace(tmp, str(_REGISTRY_FILE))
    except Exception as e:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        logger.warning("Could not save ingest registry: %s", e)


def _save_registry(registry: dict):
    _atomic_write_registry(registry)


def _update_source_entry(source_key: str, entry: dict):
    try:
        fresh = _load_registry()
        fresh[source_key] = entry
        _atomic_write_registry(fresh)
    except Exception as e:
        logger.warning("Could not update registry entry for %s: %s", source_key, e)


def _remove_source_entry(source_key: str) -> bool:
    try:
        fresh = _load_registry()
        had_entry = fresh.pop(source_key, None) is not None
        _atomic_write_registry(fresh)
        return had_entry
    except Exception as e:
        logger.warning("Could not remove registry entry for %s: %s", source_key, e)
        return False


def _paged_get(collection, limit: int = 5000, **kwargs) -> dict:
    """Safely get rows from ChromaDB in batches to prevent SQLite 'too many SQL variables'."""
    offset = 0
    all_ids = []
    all_metas = []
    try:
        while True:
            chunk = collection.get(limit=limit, offset=offset, **kwargs)
            ids = chunk.get("ids") or []
            if not ids:
                break
            all_ids.extend(ids)
            if "metadatas" in kwargs.get("include", []):
                all_metas.extend(chunk.get("metadatas") or [])
            if len(ids) < limit:
                break
            offset += len(ids)
        return {"ids": all_ids, "metadatas": all_metas}
    except TypeError:
        # Fallback for test mocks or collection wrappers that don't support limit/offset
        return collection.get(**kwargs)


def is_excluded(path: Path, source: Path, patterns: list[str]) -> bool:
    """Whether a glob pattern from `exclude` keeps this file out.

    Matched against three shapes, because callers reach for all three and a
    pattern that quietly matches nothing looks exactly like a clean folder:
    the path relative to source (`Logs/*`), the file name (`*.log`), and any
    single path component (`Logs`).
    """
    if not patterns:
        return False
    try:
        rel = path.relative_to(source).as_posix()
    except ValueError:
        rel = path.name
    parts = rel.split("/")
    for p in patterns:
        if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(path.name, p):
            return True
        if any(fnmatch.fnmatch(part, p) for part in parts):
            return True
    return False


class IngestManager:
    """Index external files into the vault's ChromaDB without touching them on disk.

    External results are tagged folder='_external' so search_vault can distinguish
    them from vault notes. Each file's absolute path is the ChromaDB document ID,
    so re-indexing the same path is safe.
    """

    def __init__(self, vault_manager):
        self._vault = vault_manager
        self._cfg = vault_manager.cfg

    def ingest(self, source_path: str, recursive: bool = True, force: bool = False,
               exclude: list[str] | None = None) -> dict:
        """Index all supported files under source_path.

        source_path: absolute path to a file or directory.
        recursive: walk subdirectories (default True).
        force: re-index even if file mtime and size are unchanged (default False).
        exclude: glob patterns for paths to leave out (default none).

        On exclude: without it the only control over what gets indexed is which
        directory you point at, so a folder holding one useful document and a
        build log costs you the log in the index. Measured on a real vault: a
        `Logs/` subdirectory of MD5 manifests and file listings, 188 thousand
        lines of hashes and paths, took 19.5 minutes to embed and answered no
        question anybody would ask. The caller could only avoid it by ingesting
        each useful subfolder separately, which is a workaround, not a control.
        """
        from .extractor import DatalessFileError, SUPPORTED, UnreadableFileError, extract

        source = Path(source_path).expanduser().resolve()
        if not source.exists():
            return {"error": f"Path not found: {source_path}"}

        candidates: list[Path]
        unsupported: Counter[str] = Counter()

        patterns = [p for p in (exclude or []) if p]
        excluded: list[str] = []

        def _keep(f: Path) -> bool:
            if is_excluded(f, source, patterns):
                excluded.append(f.name)
                return False
            if f.suffix.lower() in SUPPORTED:
                return True
            unsupported[f.suffix.lower() or "(sem extensão)"] += 1
            return False

        if source.is_file():
            candidates = [source] if _keep(source) else []
        else:
            pattern = "**/*" if recursive else "*"
            candidates = [f for f in source.glob(pattern) if f.is_file() and _keep(f)]

        indexed: list[str] = []
        skipped_empty: list[str] = []
        skipped_unreadable: list[str] = []
        skipped_dataless: list[str] = []
        skipped_unchanged: list[str] = []
        errors: list[str] = []
        now = datetime.now().isoformat()

        max_chars = self._cfg.ingest_chunk_size
        overlap   = self._cfg.ingest_chunk_overlap

        registry = _load_registry()
        source_key = str(source)
        source_meta = registry.get(source_key, {})
        cached_files = source_meta.get("files", {})
        new_cached_files = {}

        for f in candidates:
            f_str = str(f)
            try:
                st = f.stat()
                f_mtime = st.st_mtime
                f_size = st.st_size
            except OSError as e:
                logger.warning("Stat failed for %s: %s", f.name, e)
                skipped_unreadable.append(f.name)
                errors.append(f"{f.name}: {e}")
                continue

            # Incremental skip: check if file has already been indexed and is unmodified
            if not force and f_str in cached_files:
                c_info = cached_files[f_str]
                if isinstance(c_info, (list, tuple)) and len(c_info) >= 2:
                    if c_info[0] == f_mtime and c_info[1] == f_size:
                        # Counted as unchanged, NOT as indexed. Reporting a skip
                        # as an index made "N arquivos reingeridos" indistinguishable
                        # from "N arquivos already present": the number stayed
                        # plausible on a run that embedded nothing at all, which is
                        # the one case where the operator needs to know.
                        skipped_unchanged.append(f_str)
                        new_cached_files[f_str] = [f_mtime, f_size]
                        continue

            try:
                content = extract(f)
                if not content or not content.strip():
                    skipped_empty.append(f.name)
                    continue

                chunks = chunk_text(content, max_chars=max_chars, overlap=overlap)
                # Ingested files are the bulk of a real index (92.8% of rows on
                # the deployment that asked for client scoping), so a filter that
                # skipped them would reach a fourteenth of the corpus and read as
                # "that client has almost nothing". Derived from the configured
                # roots only; no root, no client, never a guess.
                client = client_from_path(
                    f_str,
                    getattr(self._cfg, "client_path_roots", None),
                    getattr(self._cfg, "client_aliases", None),
                )
                # Drop this file's previous rows before writing the new ones.
                # Upsert alone cannot express "and nothing else": ids are
                # `path::chunk_N`, or the bare path when a file yields a single
                # chunk, so a re-ingest producing FEWER chunks than last time
                # leaves every id above the new count behind -- and when the new
                # count is 1 the id shape changes, orphaning all of them. The
                # mirror case is just as real and easier to miss: growing from
                # exactly one chunk to several leaves the bare-path id behind,
                # because nothing writes that id again. Any change to the chunk
                # count can strand rows; only a same-count re-ingest is safe.
                # Measured: moving ingest_chunk_size from 1000 back to 4000 left
                # 3784 stale rows against 1193 live ones, still answering
                # searches from a fragmentation no longer in use.
                # reindex_vault already deletes by path for the same reason.
                # Placed after extraction succeeds: a failed extract must not
                # take the previous rows down with it.
                self._drop_rows_for_path(f_str)
                for i, chunk in enumerate(chunks):
                    chunk_id = f"{f}::chunk_{i}" if len(chunks) > 1 else str(f)
                    meta = {
                        "title":         f.stem,
                        "path":          f_str,
                        "folder":        "_external",
                        "source_folder": source_key,
                        "ingested_at":   now,
                        "is_external":   "true",
                        "chunk":         str(i),
                        "total_chunks":  str(len(chunks)),
                    }
                    if client:
                        meta["client"] = client
                    self._vault.index_note(chunk, meta, doc_id=chunk_id)
                indexed.append(f_str)
                new_cached_files[f_str] = [f_mtime, f_size]
            except DatalessFileError as e:
                logger.warning("Dataless file %s: %s", f.name, e)
                skipped_dataless.append(f.name)
                errors.append(f"{f.name}: {e}")
            except UnreadableFileError as e:
                logger.warning("Unreadable file %s: %s", f.name, e)
                skipped_unreadable.append(f.name)
                errors.append(f"{f.name}: {e}")
            except Exception as e:
                logger.warning("Ingest error %s: %s", f.name, e)
                skipped_unreadable.append(f.name)
                errors.append(f"{f.name}: {e}")

        merged_files = dict(cached_files) if (not force and not source.is_file()) else {}
        merged_files.update(new_cached_files)

        source_entry = {
            "last_indexed":           now,
            "indexed_count":          len(indexed),
            "skipped_unchanged_count": len(skipped_unchanged),
            "skipped_empty_count":     len(skipped_empty),
            "skipped_unreadable_count": len(skipped_unreadable),
            "skipped_dataless_count":  len(skipped_dataless),
            "error_count":            len(errors),
            "recursive":              recursive,
            "exclude":                patterns if patterns else None,
            "files":                  merged_files,
        }
        _update_source_entry(source_key, source_entry)

        total_skipped = len(skipped_empty) + len(skipped_unreadable) + len(skipped_dataless)
        result = {
            "source":             source_key,
            "indexed":            len(indexed),
            "skipped":            total_skipped,
            "skipped_empty":      len(skipped_empty),
            "skipped_unreadable": len(skipped_unreadable),
            "skipped_dataless":   len(skipped_dataless),
            "skipped_unchanged":  len(skipped_unchanged),
            "errors":             errors,
        }
        if excluded:
            # Reported, not silent: a pattern that matches more than the caller
            # meant looks exactly like a folder with fewer files in it.
            result["excluded"] = len(excluded)
            result["excluded_files"] = excluded[:20]
        if skipped_dataless:
            result["dataless_files"] = skipped_dataless
        if unsupported:
            result["unsupported"] = dict(unsupported.most_common(12))
            result["unsupported_total"] = sum(unsupported.values())
            code_like = sum(n for ext, n in unsupported.items() if ext in _CODE_HINT_EXTS)
            if code_like:
                result["hint"] = (
                    f"{code_like} code file(s) were not indexed: ingest_folder handles "
                    "documents only. Use graph_build() to make a codebase searchable."
                )
        return result

    def _drop_rows_for_path(self, path: str) -> None:
        """Remove every indexed row for one file, whatever its chunk count was.

        Deletes on the `path` metadata rather than on reconstructed ids, because
        the id shape depends on how many chunks the PREVIOUS run produced, which
        is exactly the number this code does not have.
        """
        collection = getattr(self._vault, "collection", None)
        if collection is None:
            self._vault._ensure_ready()
            collection = getattr(self._vault, "collection", None)
        if collection is None:
            return
        try:
            collection.delete(where={"path": path})
        except Exception as e:  # pragma: no cover - depends on chromadb backend
            # Never fatal: leaving a stale row is bad, but failing the whole
            # ingest over it is worse.
            logger.warning("Could not drop previous rows for %s: %s", path, e)

    def forget(self, source_path: str) -> dict:
        """Drop everything previously ingested from source_path.

        ingest is upsert-by-absolute-path, which is safe for re-runs but leaves
        rows behind forever once the source moves or is deleted: they keep
        answering searches with paths that no longer resolve. Matches the source
        folder strictly or any file contained beneath it.
        """
        source_p = Path(source_path).expanduser().resolve()
        source_str = str(source_p)
        collection = getattr(self._vault, "collection", None)
        if collection is None:
            self._vault._ensure_ready()
            collection = getattr(self._vault, "collection", None)
        if collection is None:
            return {"error": "Vault not initialized"}

        removed = 0
        try:
            rows = _paged_get(collection, where={"is_external": "true"}, include=["metadatas"])
            ids = []
            for doc_id, meta in zip(rows.get("ids") or [], rows.get("metadatas") or []):
                meta = meta or {}
                if meta.get("source_folder") == source_str:
                    ids.append(doc_id)
                    continue
                p_str = meta.get("path", "")
                if p_str:
                    try:
                        p = Path(p_str)
                        # Exact match or parent directory containment (not loose string startswith)
                        if p == source_p or source_p in p.parents:
                            ids.append(doc_id)
                    except Exception:
                        pass
            if ids:
                # Delete in batches to avoid SQLite variable limits
                batch_size = 5000
                for i in range(0, len(ids), batch_size):
                    collection.delete(ids=ids[i:i + batch_size])
                removed = len(ids)
        except Exception as e:
            logger.warning("Ingest forget failed for %s: %s", source_str, e)
            return {"error": str(e)}

        had_entry = _remove_source_entry(source_str)
        return {"source": source_str, "removed_chunks": removed, "registry_entry_removed": had_entry}


    def status(self) -> dict:
        """Return the ingestion registry: which paths have been indexed, file counts and existence."""
        registry = _load_registry()
        sources_status = {}
        missing_sources = []
        for src, info in registry.items():
            exists = Path(src).exists()
            entry = dict(info) if isinstance(info, dict) else {"last_indexed": str(info)}
            entry["exists"] = exists
            if not exists:
                missing_sources.append(src)
            sources_status[src] = entry
        res = {"sources": sources_status, "count": len(registry)}
        if missing_sources:
            res["missing_sources"] = missing_sources
            res["hint"] = "Some ingested source folders no longer exist on disk. Run ingest_forget(<source>) to clean them."
        return res
