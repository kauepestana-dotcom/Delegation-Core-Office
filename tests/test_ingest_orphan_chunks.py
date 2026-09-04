"""Re-ingesting a file must leave nothing behind from its previous chunking.

ingest writes `path::chunk_N`, or the bare path when a file yields a single
chunk. Upsert overwrites ids it writes again and says nothing about the rest, so
a re-ingest that produces FEWER chunks than last time leaves every id above the
new count in the index, still answering searches from a fragmentation no longer
in use. Shrinking to exactly one chunk is the worst case: the id shape changes
too, so not one of the old rows is overwritten.

Found by measurement, not by reading: moving ingest_chunk_size from 1000 back to
4000 left 3784 stale rows against 1193 live ones. Growing is not safe either --
going from exactly one chunk to several strands the bare-path id, since nothing
writes that id again. Any change to the chunk count can strand rows, which is
why the growing direction gets a test of its own below rather than a comment
claiming it cannot happen.

The collection is a double on purpose. The defect is in which ids get written
and which get dropped -- arithmetic that a real ChromaDB would only make slower
to observe.
"""

from pathlib import Path

import pytest

from delegation_core.config import Config
from delegation_core.ingest import IngestManager


class FakeCollection:
    """Just enough ChromaDB to see which rows survive a re-ingest."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def upsert(self, ids, documents=None, metadatas=None):  # pragma: no cover
        for i, doc_id in enumerate(ids):
            self.rows[doc_id] = (metadatas or [{}] * len(ids))[i]

    def delete(self, ids=None, where=None):
        if ids:
            for doc_id in ids:
                self.rows.pop(doc_id, None)
        if where:
            (field, value), = where.items()
            for doc_id in [k for k, m in self.rows.items() if m.get(field) == value]:
                del self.rows[doc_id]

    def ids_for(self, path: str) -> set[str]:
        return {k for k, m in self.rows.items() if m.get("path") == path}


class FakeVault:
    def __init__(self, cfg):
        self.cfg = cfg
        self.collection = FakeCollection()

    def _ensure_ready(self):
        return None

    def index_note(self, content, meta, doc_id=None):
        self.collection.rows[doc_id] = dict(meta)


def _manager(chunk_size: int):
    cfg = Config()
    cfg.ingest_chunk_size = chunk_size
    cfg.ingest_chunk_overlap = 200
    cfg.client_path_roots = []
    cfg.client_aliases = {}
    return IngestManager(FakeVault(cfg))


def _source(tmp_path: Path) -> Path:
    # Long enough that a small chunk size produces many rows and a large one
    # produces exactly one -- the transition that orphans everything.
    (tmp_path / "doc.md").write_text("palavra " * 500, encoding="utf-8")
    return tmp_path


def test_shrinking_to_a_single_chunk_leaves_no_orphans(tmp_path):
    src = _source(tmp_path)
    target = str(src / "doc.md")

    small = _manager(500)
    small.ingest(str(src))
    antes = small._vault.collection.ids_for(target)
    assert len(antes) > 1, "fixture must produce several chunks to be meaningful"
    assert any("::chunk_" in i for i in antes)

    # Same collection, new chunk size: this is the re-ingest under test.
    big = IngestManager(small._vault)
    big._cfg.ingest_chunk_size = 100_000
    big.ingest(str(src), force=True)

    depois = big._vault.collection.ids_for(target)
    assert depois == {target}, (
        f"esperava só o id nu do arquivo, sobraram {sorted(depois - {target})}")


def test_shrinking_to_fewer_chunks_leaves_no_orphans(tmp_path):
    src = _source(tmp_path)
    target = str(src / "doc.md")

    small = _manager(400)
    small.ingest(str(src))
    n_antes = len(small._vault.collection.ids_for(target))

    big = IngestManager(small._vault)
    big._cfg.ingest_chunk_size = 1500
    big.ingest(str(src), force=True)

    depois = big._vault.collection.ids_for(target)
    assert len(depois) < n_antes, "fixture must actually shrink the chunk count"
    indices = sorted(int(i.rsplit("::chunk_", 1)[1]) for i in depois if "::chunk_" in i)
    assert indices == list(range(len(indices))), (
        f"índices devem ser contíguos a partir de 0, vieram {indices}")


def test_growing_the_chunk_count_still_works(tmp_path):
    """Growing was assumed safe and is not.

    Going from one chunk to several strands the bare-path id: the new rows are
    all `path::chunk_N`, so nothing overwrites it. This test failed against the
    unfixed code, which is how the assumption was caught.
    """
    src = _source(tmp_path)
    target = str(src / "doc.md")

    big = _manager(100_000)
    big.ingest(str(src))
    assert big._vault.collection.ids_for(target) == {target}

    small = IngestManager(big._vault)
    small._cfg.ingest_chunk_size = 400
    small.ingest(str(src), force=True)

    depois = small._vault.collection.ids_for(target)
    assert len(depois) > 1
    assert target not in depois, "o id nu da rodada anterior ficou órfão"


def test_drop_is_survivable_without_a_collection(tmp_path):
    """A vault that never initialised must not turn a warning into a crash."""
    manager = _manager(500)
    manager._vault.collection = None
    manager._drop_rows_for_path(str(tmp_path / "doc.md"))  # não levanta
