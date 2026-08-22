"""Tests for ingest idempotency: byte-identical re-ingests are skipped.

The pipeline's `_already_ingested` fast-path compares the stored
``object_key`` (which embeds the PDF sha) against the key this run would
produce. If they match, the run is a no-op unless ``force=True``.
"""

from __future__ import annotations

from pathlib import Path

from uk_regs_ingest.pipeline import IngestPipeline


class _FakeDocRepo:
    """Fake DocumentRepository that records calls and returns a canned key."""

    def __init__(self, stored_key: str | None) -> None:
        self.stored_key = stored_key
        self.calls: list[tuple[str, str]] = []

    def get_version_object_key(self, doc_id: str, version: str) -> str | None:
        self.calls.append((doc_id, version))
        return self.stored_key


class _ExplodingRepo:
    """Repository whose lookup raises — pipeline must fail open (process)."""

    def get_version_object_key(self, doc_id: str, version: str) -> str | None:
        raise RuntimeError("db down")


class _FakeObjectStore:
    """Minimal object store — accepts puts, reports nothing exists."""

    def exists(self, key: str) -> bool:
        return False

    def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        pass


def _make_pipeline(doc_repo) -> IngestPipeline:
    return IngestPipeline(
        object_store=_FakeObjectStore(),  # type: ignore[arg-type]
        doc_repo=doc_repo,  # type: ignore[arg-type]
    )


def test_already_ingested_true_when_keys_match():
    from uk_regs_knowledge.storage.object_store import document_key

    key = document_key("doc-x", "v1", "abc123")
    p = _make_pipeline(_FakeDocRepo(stored_key=key))
    assert p._already_ingested("doc-x", "v1", "abc123") is True


def test_already_ingested_false_when_sha_differs():
    p = _make_pipeline(_FakeDocRepo(stored_key="documents/doc-x/v1/sha-old/source.pdf"))
    assert p._already_ingested("doc-x", "v1", "sha-new") is False


def test_already_ingested_false_when_never_ingested():
    p = _make_pipeline(_FakeDocRepo(stored_key=None))
    assert p._already_ingested("doc-x", "v1", "abc123") is False


def test_already_ingested_fails_open_on_db_error():
    p = _make_pipeline(_ExplodingRepo())
    assert p._already_ingested("doc-x", "v1", "abc123") is False


def test_run_skips_identical_reingest(tmp_path: Path):
    """A byte-identical re-ingest returns skipped=True without parsing."""
    import uk_regs_ingest.pipeline as pl
    from uk_regs_core.ids import doc_id_from_title, sha256_of
    from uk_regs_knowledge.storage.object_store import document_key

    pdf_bytes = b"%PDF-fake"
    doc_id = doc_id_from_title("Doc X")
    sha = sha256_of(pdf_bytes)
    key = document_key(doc_id, "v1", sha)
    p = _make_pipeline(_FakeDocRepo(stored_key=key))

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(pdf_bytes)

    called = {"parse": 0}
    orig_parse = pl.parse_pdf

    def spy_parse(*args, **kwargs):
        called["parse"] += 1
        return orig_parse(*args, **kwargs)

    pl.parse_pdf = spy_parse
    try:
        result = p.run(pdf, title="Doc X", version="v1", skip_llm=True)
    finally:
        pl.parse_pdf = orig_parse

    assert result.skipped is True
    assert result.rules_extracted == 0
    assert called["parse"] == 0  # fast-path returned before parsing
    assert "skipped" in result.summary()


def test_run_force_overrides_skip(tmp_path: Path):
    """force=True processes even when the identical sha was ingested before."""
    import uk_regs_ingest.pipeline as pl
    from uk_regs_core.ids import doc_id_from_title, sha256_of
    from uk_regs_knowledge.storage.object_store import document_key

    pdf_bytes = b"%PDF-fake"
    doc_id = doc_id_from_title("Doc X")
    sha = sha256_of(pdf_bytes)
    key = document_key(doc_id, "v1", sha)
    p = _make_pipeline(_FakeDocRepo(stored_key=key))

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(pdf_bytes)

    class _StubParsed:
        pages = 1
        blocks = []
        figure_images: dict = {}

    class _NoopRepo:
        """DocumentRepository stand-in so no real DB is contacted."""

        def __init__(self, *args, **kwargs) -> None: ...

        def upsert_document(self, doc) -> None: ...

        def upsert_version(self, v) -> None: ...

    orig_parse, orig_repo = pl.parse_pdf, pl.DocumentRepository
    pl.parse_pdf = lambda *a, **k: _StubParsed()
    pl.DocumentRepository = _NoopRepo
    try:
        result = p.run(pdf, title="Doc X", version="v1", skip_llm=True, force=True)
    finally:
        pl.parse_pdf = orig_parse
        pl.DocumentRepository = orig_repo

    assert result.skipped is False
    assert result.pages == 1  # parsing actually ran
