"""Client scoping — the filter a vault organised by client did not have.

Searching one client's retention metrics returned six of ten results from a
different client, and no parameter could exclude them. The failure modes of the
obvious fix are all in here: normalising only on the way in, reaching only the
7% of rows that are vault notes, and guessing a client from a path shape.
"""

import pytest

from delegation_core.config import Config
from delegation_core.vault import VaultManager, client_from_path, client_slug


# ── normalisation: one bucket, both sides ────────────────────────────────────

def test_case_variants_fold_to_one_slug():
    """111 notes said Gazin and 13 said gazin; an exact-equality filter for
    either missed the other's rows and said so by returning fewer results."""
    assert client_slug("Gazin") == client_slug("gazin") == client_slug("GAZIN") == "gazin"


def test_accents_and_punctuation_fold():
    assert client_slug("Grupo Angelus") == "grupo-angelus"
    assert client_slug("Aliter & Co.") == "aliter-co"
    assert client_slug("  China Gate  ") == "china-gate"


def test_folding_does_not_invent_equality():
    """"Campo Incorporadora" and "campo" are different strings. Deciding they
    are one client is a judgement about the data, so it belongs in config."""
    assert client_slug("Campo Incorporadora") != client_slug("campo")


def test_an_alias_map_merges_what_folding_cannot():
    aliases = {"campo-incorporadora": "campo"}
    assert client_slug("Campo Incorporadora", aliases) == "campo"
    assert client_slug("campo", aliases) == "campo"


def test_empty_and_punctuation_only_values_yield_nothing():
    assert client_slug("") == ""
    assert client_slug("---") == ""
    assert client_slug(None) == ""


# ── path derivation: reach the 93%, but never guess ─────────────────────────

def test_a_configured_root_yields_the_segment_beneath_it():
    assert client_from_path("/Work/Oksigen/Gazin/deck.pdf", ["/Work/Oksigen"]) == "gazin"


def test_no_configured_root_means_no_client():
    """A wrong label is worse than none: unlabelled still surfaces in an
    unfiltered search, mislabelled is silently excluded from the right filter."""
    assert client_from_path("/Work/Oksigen/Gazin/deck.pdf", []) == ""
    assert client_from_path("/Work/Oksigen/Gazin/deck.pdf", None) == ""


def test_a_non_matching_root_is_not_forced():
    assert client_from_path("/Elsewhere/Gazin/deck.pdf", ["/Work/Oksigen"]) == ""


def test_a_file_directly_in_the_root_has_no_client_segment():
    assert client_from_path("/Work/Oksigen/loose.pdf", ["/Work/Oksigen"]) == ""


def test_derivation_normalises_like_everything_else():
    assert client_from_path("/W/O/Grupo Angelus/x.pdf", ["/W/O"]) == "grupo-angelus"
    assert client_from_path("/W/O/GAZIN/x.pdf", ["/W/O"],
                            {"gazin": "gazin-holding"}) == "gazin-holding"


# ── end to end, against a real collection ───────────────────────────────────

class _Embedder:
    """Deterministic bag-of-words vectors — no model, no GPU."""

    def __call__(self, input):
        out = []
        for text in input:
            v = [0.0] * 16
            for w in str(text).lower().split():
                v[hash(w) % 16] += 1.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out

    # chromadb 1.5.9 routes queries through embed_query and documents through
    # embed_documents; an object with only __call__ indexes fine and then fails
    # at query time with "has no attribute 'embed_query'".
    # Keyword-named `input`, which is how chromadb calls them.
    def embed_documents(self, input):
        return self(input)

    def embed_query(self, input):
        if isinstance(input, str):
            return self([input])[0]
        return self(list(input))

    def name(self):
        return "probe"


@pytest.fixture
def vm(tmp_path):
    import chromadb
    cfg = Config(vault_path=str(tmp_path), vault_folders=["Notes"],
                 client_path_roots=["/Work/Oksigen"],
                 client_aliases={"campo-incorporadora": "campo"},
                 search_threshold=0.0)
    (tmp_path / "Notes").mkdir()
    v = VaultManager(cfg)
    client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
    v.collection = client.get_or_create_collection(
        "probe_client", embedding_function=_Embedder(),
        metadata={"hnsw:space": "cosine"})
    v._initialized = True
    v._disk_state = None
    v._read_disk_state = lambda: None
    v._ensure_ready = lambda: None
    return v


def _note(vm, rel, body, client=None):
    fm = f"---\nclient: {client}\n---\n\n" if client else ""
    (vm.cfg.vault / rel).parent.mkdir(parents=True, exist_ok=True)
    (vm.cfg.vault / rel).write_text(fm + body, encoding="utf-8")
    vm.index_note(fm + body, vm.note_metadata(rel, rel, "Notes", fm + body))


def test_frontmatter_client_is_promoted_and_normalised(vm):
    _note(vm, "Notes/a.md", "retention expansion metrics", client="Gazin")
    _note(vm, "Notes/b.md", "retention expansion metrics", client="gazin")
    got = vm.collection.get(include=["metadatas"])
    slugs = {m.get("client") for m in got["metadatas"]}
    assert slugs == {"gazin"}, "both spellings must land in one bucket"


def test_a_note_without_a_client_carries_none(vm):
    _note(vm, "Notes/c.md", "retention expansion metrics")
    meta = vm.collection.get(include=["metadatas"])["metadatas"][0]
    assert "client" not in meta or not meta.get("client")


def test_the_filter_excludes_other_clients(vm):
    """The reported symptom: ten results, six of them another client."""
    _note(vm, "Notes/g.md", "retention expansion metrics", client="Gazin")
    for i in range(6):
        _note(vm, f"Notes/c{i}.md", "retention expansion metrics", client="Campo Incorporadora")

    unfiltered = vm.search("retention expansion metrics", limit=10)
    assert len(unfiltered) == 7

    only_gazin = vm.search("retention expansion metrics", limit=10, client="gazin")
    assert [h["path"] for h in only_gazin] == ["Notes/g.md"]


def test_the_query_side_is_normalised_too(vm):
    """A patch that normalised only on write leaves client="Gazin" matching
    nothing it just wrote — which reads as "that client has no notes"."""
    _note(vm, "Notes/g.md", "retention expansion metrics", client="gazin")
    assert vm.search("retention expansion metrics", limit=5, client="Gazin")
    assert vm.search("retention expansion metrics", limit=5, client="  GAZIN ")


def test_the_alias_map_applies_on_both_sides(vm):
    _note(vm, "Notes/c.md", "retention expansion metrics", client="Campo Incorporadora")
    assert vm.search("retention expansion metrics", limit=5, client="campo")


def test_client_composes_with_scope_rather_than_replacing_it(vm):
    _note(vm, "Notes/g.md", "retention expansion metrics", client="Gazin")
    vm.index_note("retention expansion metrics",
                  {"title": "ext", "path": "/Work/Oksigen/Gazin/deck.pdf",
                   "folder": "_external", "is_external": "true",
                   "client": "Gazin"}, doc_id="/Work/Oksigen/Gazin/deck.pdf")

    both = vm.search("retention expansion metrics", limit=10, client="gazin")
    assert len(both) == 2

    notes_only = vm.search("retention expansion metrics", limit=10,
                           scope="notes", client="gazin")
    assert [h["path"] for h in notes_only] == ["Notes/g.md"]


def test_an_unknown_client_returns_nothing_rather_than_everything(vm):
    _note(vm, "Notes/g.md", "retention expansion metrics", client="Gazin")
    assert vm.search("retention expansion metrics", limit=10, client="nobody") == []


def test_hits_carry_their_client_even_unfiltered(vm):
    """So a caller can see that six of ten are another client, which is what
    tells it a filter was called for."""
    _note(vm, "Notes/g.md", "retention expansion metrics", client="Gazin")
    hit = vm.search("retention expansion metrics", limit=5)[0]
    assert hit["client"] == "gazin"


def test_stats_lists_the_clients_present(vm):
    _note(vm, "Notes/g.md", "retention expansion metrics", client="Gazin")
    _note(vm, "Notes/c.md", "retention expansion metrics", client="Campo Incorporadora")
    counts = vm._client_counts()
    assert counts == {"gazin": 1, "campo": 1}


def test_client_counts_count_documents_not_chunks(vm):
    """A chunked 160-page deck must not make one client look like a hundred."""
    vm.cfg.vault_chunk_size = 60
    vm.cfg.vault_chunk_overlap = 10
    _note(vm, "Notes/big.md", "retention expansion metrics " * 40, client="Gazin")
    rows, _ = vm._index_counts()
    assert rows > 1, "the fixture must actually produce several chunks"
    assert vm._client_counts() == {"gazin": 1}


def test_frontmatter_custom_metadata_is_promoted_and_persists(vm):
    content = "---\ntitle: Custom Note\nstatus: active\nproject: Atlas\ncategory: infrastructure\n---\n\nSample content."
    rel = "Notes/custom.md"
    (vm.cfg.vault / rel).parent.mkdir(parents=True, exist_ok=True)
    (vm.cfg.vault / rel).write_text(content, encoding="utf-8")
    vm.index_note(content, vm.note_metadata(rel, "Custom Note", "Notes", content))

    got = vm.collection.get(include=["metadatas"])
    meta = [m for m in got["metadatas"] if m.get("path") == rel][0]
    assert meta.get("status") == "active"
    assert meta.get("project") == "Atlas"
    assert meta.get("category") == "infrastructure"


def test_client_from_path_aceita_caminho_windows():
    """Caminho nativo do Windows deve derivar cliente igual ao POSIX.

    client_from_path usa PurePosixPath, que so quebra componentes em '/'. Um
    caminho Windows inteiro virava UM componente, relative_to() levantava
    ValueError e a funcao devolvia "" para todo arquivo -- client_path_roots
    ficava inteiramente inerte no Windows, sem erro nenhum, indistinguivel de
    "esse cliente nao tem documentos".

    Toda asserção existente neste arquivo usa caminho POSIX, que foi como o
    defeito passou. Os quatro cruzamentos abaixo cobrem a mistura de
    separadores entre raiz e arquivo, que e o caso real: config escrita a mao
    de um lado, caminho gravado pelo ingest do outro.
    """
    raiz_win   = r"C:\Work\Oksigen"
    arq_win    = r"C:\Work\Oksigen\Gazin\deck.pdf"
    raiz_posix = "C:/Work/Oksigen"
    arq_posix  = "C:/Work/Oksigen/Gazin/deck.pdf"

    assert client_from_path(arq_win,   [raiz_win])   == "gazin"
    assert client_from_path(arq_posix, [raiz_win])   == "gazin"
    assert client_from_path(arq_win,   [raiz_posix]) == "gazin"
    assert client_from_path(arq_posix, [raiz_posix]) == "gazin"


def test_client_from_path_windows_preserva_as_guardas():
    """A normalizacao de separador nao pode afrouxar as duas regras de recusa."""
    raiz = r"C:\Work\Oksigen"
    # arquivo direto na raiz nao tem segmento de cliente para ler
    assert client_from_path(r"C:\Work\Oksigen\solto.pdf", [raiz]) == ""
    # raiz que nao casa continua sem cliente
    assert client_from_path(r"C:\Outro\Gazin\deck.pdf", [raiz]) == ""
