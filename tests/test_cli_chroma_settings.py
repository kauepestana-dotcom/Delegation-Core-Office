"""Every Chroma client on one path must be built with the same Settings.

`delegation-core embed-model <model> --reindex` downloaded 2.2 GB, ran, printed
"0 notas indexadas", logged 2153 "was NOT indexed" lines and exited 0. Nothing
raised. The cause was two clients on one path with different settings:
cmd_embed_model probed the row count with a bare PersistentClient, then
_reindex_everything built a VaultManager, whose client passes
Settings(anonymized_telemetry=False). chromadb caches one System per path for
the life of the process and rejects the second client, so every index_note fell
through to "no collection" and the command reported success over an empty
collection.

The behavioural test pins the chromadb rule the defect rode on. The source test
is the one that actually stops the regression: the rule is invisible at every
individual call site, and only shows up when two of them run in one process --
which no test did, because the CLI's reindex path had none.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "delegation_core"
SETTINGS_CALL = "chromadb.Settings(anonymized_telemetry=False)"


def _clear_chroma_cache():
    from chromadb.api.client import SharedSystemClient
    SharedSystemClient.clear_system_cache()


@pytest.fixture(autouse=True)
def _isolated_chroma_system():
    """chromadb's System cache is process-global, so a leaked entry from one
    test makes the next one pass or fail for the wrong reason."""
    _clear_chroma_cache()
    yield
    _clear_chroma_cache()


# ── the chromadb rule the defect rode on ─────────────────────────────────────

def test_second_client_with_different_settings_is_rejected(tmp_path):
    """This is the whole mechanism. If a future chromadb stops rejecting the
    mismatch, this test fails and the invariant below can be relaxed on
    purpose rather than forgotten."""
    import chromadb

    chromadb.PersistentClient(path=str(tmp_path))
    with pytest.raises(Exception, match="already exists"):
        chromadb.PersistentClient(
            path=str(tmp_path),
            settings=chromadb.Settings(anonymized_telemetry=False))


def test_matching_settings_allow_a_second_client(tmp_path):
    import chromadb

    settings = chromadb.Settings(anonymized_telemetry=False)
    first = chromadb.PersistentClient(path=str(tmp_path), settings=settings)
    second = chromadb.PersistentClient(path=str(tmp_path), settings=settings)
    first.get_or_create_collection("probe")
    assert [c.name for c in second.list_collections()] == ["probe"]


# ── the invariant that stops the regression ──────────────────────────────────

def _persistent_client_calls(path: Path):
    """Every chromadb.PersistentClient(...) call in one module, as AST nodes."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "PersistentClient":
            yield node


@pytest.mark.parametrize("module", ["cli.py", "vault.py"])
def test_every_persistent_client_passes_settings(module):
    """cli.py had three bare ones while vault.py and doctor.py passed settings.
    Any single one of the three was enough to poison a whole command.

    doctor.py is absent on purpose: it builds its client inside a script it
    embeds as a string and runs in a subprocess, so there is no Call node to
    walk. The string-level test below is what covers it -- and a subprocess is
    a fresh System cache anyway, which is the one place the mismatch is
    harmless."""
    path = SRC / module
    calls = list(_persistent_client_calls(path))
    assert calls, f"expected at least one PersistentClient call in {module}"
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords}
        assert "settings" in kwargs, (
            f"{module}:{call.lineno} builds PersistentClient without settings; "
            f"a second client on that path in the same process will be rejected")


@pytest.mark.parametrize("module", ["cli.py", "vault.py", "doctor.py"])
def test_settings_shape_is_identical_everywhere(module):
    """Passing *some* settings is not enough: they have to be the same ones,
    or the mismatch is back with an extra step."""
    source = (SRC / module).read_text(encoding="utf-8")
    assert SETTINGS_CALL in source, (
        f"{module} does not use {SETTINGS_CALL}; a divergent Settings object "
        f"reintroduces the mismatch this module was fixed for")
