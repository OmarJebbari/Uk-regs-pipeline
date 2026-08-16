"""
Phase 5-1 — Source Monitoring & Auto-Ingestion tests.

Covers:
    * Mock + Local fetchers (list + fetch)
    * Diff engine (new / update / skip / force)
    * State store (in-memory + postgres-shaped)
    * SourceMonitor end-to-end (with a fake ingest pipeline)
    * Scheduler run_once()
    * API surface (/sources, /sources/poll)

Run:
    uv run --directory . python -m pytest tests/test_sourcing.py -v
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

os.environ.setdefault("UK_REGS_BACKEND", "mock")

from uk_regs_sourcing.fetchers import (  # noqa: E402
    HttpFetcher,
    LocalFetcher,
    MockFetcher,
    UnsafeUrlError,
    _validate_public_url,
    make_fetcher,
)
from uk_regs_sourcing.models import (  # noqa: E402
    SourceEntry,
    SourceRegistry,
    SourceState,
)
from uk_regs_sourcing.monitor import (  # noqa: E402
    MonitorResult,
    SourceMonitor,
    SourceRunResult,
)
from uk_regs_sourcing.registry import load_registry  # noqa: E402
from uk_regs_sourcing.state import (  # noqa: E402
    InMemoryStateStore,
    reset_state_store,
    set_state_store,
)


# =========================================================================== #
#  Fixtures
# =========================================================================== #
@pytest.fixture(autouse=True)
def _isolate_state_store():
    """Each test gets a fresh in-memory state store (no global leakage)."""
    store = InMemoryStateStore()
    set_state_store(store)
    yield
    reset_state_store()
    MockFetcher.reset()


def _entry(**overrides) -> SourceEntry:
    defaults = {
        "source_id": "test-src",
        "title": "Test Source",
        "fetcher": "mock",
        "doc_type": "approved_document",
        "default_version": "2026-ed1",
        # 0 => never throttle; individual tests override to test throttling.
        "poll_interval_minutes": 0,
    }
    defaults.update(overrides)
    return SourceEntry(**defaults)


def _state(document_id="doc-a", sha256="aaa", version="v1") -> SourceState:
    return SourceState(
        source_id="test-src",
        document_id=document_id,
        title=document_id.replace("-", " ").title(),
        version=version,
        url=f"mock://test-src/{document_id}",
        sha256=sha256,
        size_bytes=999,
        published_at=datetime.now(UTC),
    )


class _FakePipelineResult:
    def __init__(self, change_set_id="CS_FAKE_001"):
        self.change_set_id = change_set_id


class _FakePipeline:
    """Stand-in for IngestPipeline — records calls, never touches PDFs."""

    calls: list[Path] = []

    def run(self, path, **kwargs):  # noqa: ANN003
        type(self).calls.append(path)
        return _FakePipelineResult()


# =========================================================================== #
#  Fetchers
# =========================================================================== #
class TestMockFetcher:
    def test_seed_and_list(self):
        MockFetcher.seed("test-src", {"doc-a": b"%PDF-1.4 a", "doc-b": b"%PDF-1.4 b"})
        f = MockFetcher(_entry())
        docs = f.list_documents()
        assert len(docs) == 2
        ids = {d.document_id for d in docs}
        assert ids == {"doc-a", "doc-b"}

    def test_fetch_returns_bytes(self):
        content = b"%PDF-1.4 hello world"
        MockFetcher.seed("test-src", {"doc-a": content})
        f = MockFetcher(_entry())
        state = f.list_documents()[0]
        result = f.fetch(state)
        assert result.content == content
        assert result.source_id == "test-src"
        assert len(result.sha256) == 64  # sha256 hex

    def test_empty_when_unseeded(self):
        f = MockFetcher(_entry())
        assert f.list_documents() == []


class TestLocalFetcher:
    def test_scans_pdf_directory(self, tmp_path: Path):
        (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4 a")
        (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4 b")
        (tmp_path / "not-a-pdf.txt").write_text("ignore me")

        entry = _entry(fetcher="local", path=str(tmp_path))
        fetcher = LocalFetcher(entry)
        docs = fetcher.list_documents()
        assert len(docs) == 2
        assert all(d.url.endswith(".pdf") for d in docs)

    def test_missing_path_is_safe(self):
        entry = _entry(fetcher="local", path="/nonexistent/xyz")
        fetcher = LocalFetcher(entry)
        assert fetcher.list_documents() == []  # logs warning, no crash

    def test_fetch_reads_file(self, tmp_path: Path):
        pdf = tmp_path / "rule.pdf"
        content = b"%PDF-1.4 real content"
        pdf.write_bytes(content)

        entry = _entry(fetcher="local", path=str(tmp_path))
        fetcher = LocalFetcher(entry)
        state = fetcher.list_documents()[0]
        result = fetcher.fetch(state)
        assert result.content == content


class TestFetcherFactory:
    def test_make_http_fetcher(self):
        f = make_fetcher(_entry(fetcher="http", url="https://example.com"))
        assert f.kind == "http"

    def test_make_local_fetcher(self):
        f = make_fetcher(_entry(fetcher="local", path="."))
        assert f.kind == "local"

    def test_make_mock_fetcher(self):
        f = make_fetcher(_entry(fetcher="mock"))
        assert f.kind == "mock"

    def test_unknown_fetcher_raises(self):
        with pytest.raises(ValueError, match="unknown fetcher"):
            make_fetcher(_entry(fetcher="nonexistent"))


# =========================================================================== #
#  SSRF guard — HttpFetcher only talks to public http(s) hosts
# =========================================================================== #
class TestSsrfGuard:
    def test_loopback_ip_rejected(self):
        with pytest.raises(UnsafeUrlError, match="non-public"):
            _validate_public_url("http://127.0.0.1:8000/doc.pdf", allow_private=False)

    def test_rfc1918_rejected(self):
        for host in ("10.0.0.5", "192.168.1.1", "172.16.0.9"):
            with pytest.raises(UnsafeUrlError, match="non-public"):
                _validate_public_url(f"http://{host}/doc.pdf", allow_private=False)

    def test_cloud_metadata_endpoint_rejected(self):
        with pytest.raises(UnsafeUrlError, match="non-public"):
            _validate_public_url(
                "http://169.254.169.254/latest/meta-data/iam/", allow_private=False
            )

    def test_file_scheme_rejected(self):
        with pytest.raises(UnsafeUrlError, match="scheme"):
            _validate_public_url("file:///etc/passwd", allow_private=False)

    def test_ipv6_loopback_rejected(self):
        with pytest.raises(UnsafeUrlError, match="non-public"):
            _validate_public_url("http://[::1]/doc.pdf", allow_private=False)

    def test_hostname_resolving_private_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "uk_regs_sourcing.fetchers._resolve_host", lambda host: ["10.1.2.3"]
        )
        with pytest.raises(UnsafeUrlError, match="non-public"):
            _validate_public_url("http://internal.local/doc.pdf", allow_private=False)

    def test_mixed_public_private_records_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "uk_regs_sourcing.fetchers._resolve_host",
            lambda host: ["93.184.216.34", "10.0.0.1"],
        )
        with pytest.raises(UnsafeUrlError, match="non-public"):
            _validate_public_url("http://mixed.example/doc.pdf", allow_private=False)

    def test_public_literal_ip_allowed(self):
        _validate_public_url("https://93.184.216.34/doc.pdf", allow_private=False)  # no raise

    def test_allow_private_bypass(self):
        _validate_public_url("http://127.0.0.1:8000/doc.pdf", allow_private=True)  # no raise


class _FakeResp:
    def __init__(self, url: str, location: str | None = None):
        self.url = url
        self.headers = {"location": location} if location else {}
        self.content = b"%PDF-1.4 fake"

    @property
    def is_redirect(self) -> bool:
        return "location" in self.headers

    def raise_for_status(self) -> None:
        pass


class _FakeClient:
    """Stands in for httpx.Client — scripted responses, no network."""

    responses: list[_FakeResp] = []

    def __init__(self, *args, **kwargs):  # noqa: ANN002, ANN003
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):  # noqa: ANN001
        return type(self).responses.pop(0)


class TestSsrfRedirectValidation:
    def test_redirect_to_metadata_endpoint_blocked(self, monkeypatch):
        # Initial host looks public (resolve to a public IP)…
        monkeypatch.setattr(
            "uk_regs_sourcing.fetchers._resolve_host", lambda host: ["93.184.216.34"]
        )
        # …but the response redirects to the cloud metadata endpoint.
        _FakeClient.responses = [
            _FakeResp("https://pub.example/doc.pdf", location="http://169.254.169.254/creds"),
        ]
        monkeypatch.setattr("httpx.Client", _FakeClient)

        fetcher = HttpFetcher(_entry(fetcher="http", url="https://pub.example/doc.pdf"))
        with pytest.raises(UnsafeUrlError, match="non-public"):
            fetcher._http_get("https://pub.example/doc.pdf", accept="application/pdf")

    def test_redirect_loop_raises(self, monkeypatch):
        monkeypatch.setattr(
            "uk_regs_sourcing.fetchers._resolve_host", lambda host: ["93.184.216.34"]
        )
        _FakeClient.responses = [
            _FakeResp("https://pub.example/a", location="https://pub.example/b")
        ] * 10
        monkeypatch.setattr("httpx.Client", _FakeClient)

        fetcher = HttpFetcher(_entry(fetcher="http", url="https://pub.example/a"))
        with pytest.raises(UnsafeUrlError, match="too many redirects"):
            fetcher._http_get("https://pub.example/a", accept="application/pdf")


# =========================================================================== #
#  Diff engine
# =========================================================================== #
class TestDiffEngine:
    def test_new_document_is_ingest(self):
        seen: dict[str, SourceState] = {}
        diff = SourceMonitor._diff(_state(), seen, force=False)
        assert diff.action == "ingest"
        assert "new" in diff.reason

    def test_unchanged_document_is_skip(self):
        state = _state()
        seen = {state.document_id: state}
        diff = SourceMonitor._diff(state, seen, force=False)
        assert diff.action == "skip"

    def test_changed_sha256_is_update(self):
        old = _state(sha256="old-hash")
        new = _state(sha256="new-hash")
        seen = {old.document_id: old}
        diff = SourceMonitor._diff(new, seen, force=False)
        assert diff.action == "update"

    def test_changed_version_is_update(self):
        old = _state(version="v1")
        new = _state(version="v2")
        seen = {old.document_id: old}
        diff = SourceMonitor._diff(new, seen, force=False)
        assert diff.action == "update"

    def test_changed_size_is_update(self):
        old = _state()
        old = old.model_copy(update={"size_bytes": 100})
        new = _state()
        new = new.model_copy(update={"size_bytes": 200})
        seen = {old.document_id: old}
        diff = SourceMonitor._diff(new, seen, force=False)
        assert diff.action == "update"

    def test_force_overrides_skip(self):
        state = _state()
        seen = {state.document_id: state}
        diff = SourceMonitor._diff(state, seen, force=True)
        assert diff.action == "update"
        assert "forced" in diff.reason

    def test_force_on_new_document_is_ingest(self):
        diff = SourceMonitor._diff(_state(), {}, force=True)
        assert diff.action == "ingest"


# =========================================================================== #
#  State store
# =========================================================================== #
class TestInMemoryStateStore:
    def test_mark_and_get_seen(self):
        store = InMemoryStateStore()
        state = _state()
        assert store.get_seen("test-src") == {}
        store.mark_seen("test-src", state)
        seen = store.get_seen("test-src")
        assert state.document_id in seen
        assert seen[state.document_id].sha256 == state.sha256

    def test_seen_isolates_by_source(self):
        store = InMemoryStateStore()
        store.mark_seen("src-a", _state(document_id="doc1"))
        store.mark_seen("src-b", _state(document_id="doc2"))
        assert len(store.get_seen("src-a")) == 1
        assert len(store.get_seen("src-b")) == 1

    def test_last_poll_round_trip(self):
        store = InMemoryStateStore()
        assert store.last_poll("test-src") is None
        when = datetime.now(UTC)
        store.set_last_poll("test-src", when)
        assert store.last_poll("test-src") == when


# =========================================================================== #
#  Registry
# =========================================================================== #
class TestRegistry:
    def test_load_default_registry(self):
        registry = load_registry()
        assert isinstance(registry, SourceRegistry)
        assert len(registry.sources) >= 1

    def test_enabled_sources_filters_disabled(self):
        registry = SourceRegistry(
            sources=[
                _entry(source_id="a", enabled=True),
                _entry(source_id="b", enabled=False),
            ]
        )
        enabled = registry.enabled_sources()
        assert len(enabled) == 1
        assert enabled[0].source_id == "a"

    def test_registry_get(self):
        registry = SourceRegistry(sources=[_entry(source_id="find-me")])
        assert registry.get("find-me") is not None
        assert registry.get("missing") is None


# =========================================================================== #
#  SourceMonitor (end-to-end with fake pipeline)
# =========================================================================== #
class TestSourceMonitor:
    def test_poll_source_ingests_new_documents(self):
        MockFetcher.seed("test-src", {"doc-a": b"%PDF-1.4 alpha"})
        _FakePipeline.calls.clear()

        mon = SourceMonitor(
            state_store=InMemoryStateStore(),
            pipeline_factory=_FakePipeline,
        )
        result = mon.poll_source(_entry(), force=True)

        assert isinstance(result, SourceRunResult)
        assert result.advertised == 1
        assert result.new == 1
        assert result.failed == 0
        assert "doc-a" in result.ingested_doc_ids
        assert len(_FakePipeline.calls) == 1

    def test_poll_source_skips_unchanged(self):
        MockFetcher.seed("test-src", {"doc-a": b"%PDF-1.4 alpha"})
        store = InMemoryStateStore()
        mon = SourceMonitor(state_store=store, pipeline_factory=_FakePipeline)

        # First poll: ingests
        mon.poll_source(_entry(), force=True)
        # Second poll: nothing new (no force)
        _FakePipeline.calls.clear()
        result = mon.poll_source(_entry(), force=False)
        assert result.new == 0
        assert result.skipped >= 1
        assert len(_FakePipeline.calls) == 0

    def test_poll_source_detects_updates(self):
        store = InMemoryStateStore()
        mon = SourceMonitor(state_store=store, pipeline_factory=_FakePipeline)

        # Seed v1, ingest
        MockFetcher.seed("test-src", {"doc-a": b"%PDF-1.4 v1"})
        mon.poll_source(_entry(), force=True)

        # Re-seed with different content (new sha256)
        MockFetcher.reset()
        MockFetcher.seed("test-src", {"doc-a": b"%PDF-1.4 v2 changed"})
        _FakePipeline.calls.clear()

        result = mon.poll_source(_entry(), force=True)
        assert result.updated == 1
        assert len(_FakePipeline.calls) == 1

    def test_poll_source_throttles_by_interval(self):
        store = InMemoryStateStore()
        mon = SourceMonitor(state_store=store, pipeline_factory=_FakePipeline)

        entry = _entry(poll_interval_minutes=1440)
        MockFetcher.seed("test-src", {"doc-a": b"%PDF-1.4"})
        mon.poll_source(entry, force=False)

        _FakePipeline.calls.clear()
        result = mon.poll_source(entry, force=False)
        # Should be throttled: sentinel skipped = -1, no ingest
        assert result.skipped == -1
        assert len(_FakePipeline.calls) == 0

    def test_poll_source_handles_fetch_error(self):
        # No seed -> fetch() will KeyError on the document
        entry = _entry()
        mon = SourceMonitor(state_store=InMemoryStateStore(), pipeline_factory=_FakePipeline)

        # list_documents returns [] when unseeded -> no failures, just empty
        result = mon.poll_source(entry, force=True)
        assert result.advertised == 0

    def test_poll_all_iterates_sources(self):
        MockFetcher.seed("src-a", {"d1": b"%PDF-1.4"})
        MockFetcher.seed("src-b", {"d2": b"%PDF-1.4", "d3": b"%PDF-1.4"})

        entries = [
            _entry(source_id="src-a"),
            _entry(source_id="src-b"),
            _entry(source_id="disabled", enabled=False),
        ]
        mon = SourceMonitor(state_store=InMemoryStateStore(), pipeline_factory=_FakePipeline)
        cycle = mon.poll_all(entries, force=True)

        assert isinstance(cycle, MonitorResult)
        assert cycle.finished_at is not None
        # Only 2 enabled sources polled
        assert len(cycle.sources) == 2
        assert cycle.total_ingested == 3


# =========================================================================== #
#  Scheduler — run_once()
# =========================================================================== #
class TestSchedulerRunOnce:
    def test_run_once_no_enabled_sources(self, tmp_path: Path):
        registry = tmp_path / "empty.yaml"
        registry.write_text("sources: []\n")

        from uk_regs_sourcing.scheduler import run_once

        cycle = run_once(registry_path=str(registry))
        assert cycle.total_ingested == 0
        assert cycle.finished_at is not None


# =========================================================================== #
#  API — /sources endpoints
# =========================================================================== #
class TestSourceAPI:
    def test_list_sources_endpoint(self):
        from fastapi.testclient import TestClient
        from uk_regs_api.app import app

        client = TestClient(app)
        resp = client.get("/sources")
        assert resp.status_code == 200
        body = resp.json()
        assert "count" in body
        assert isinstance(body["sources"], list)
        if body["sources"]:
            first = body["sources"][0]
            assert "source_id" in first
            assert "enabled" in first
            assert "documents_seen" in first
            assert "last_polled_at" in first

    def test_trigger_poll_endpoint(self, monkeypatch):
        """The /sources/poll endpoint delegates to scheduler.run_once."""
        from fastapi.testclient import TestClient
        from uk_regs_api.app import app

        # Monkeypatch run_once to avoid hitting the real registry/ingest
        from uk_regs_sourcing import scheduler

        def fake_run_once(**kwargs):  # noqa: ANN003
            return MonitorResult(
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                sources=[SourceRunResult(source_id="mocked", new=0)],
            )

        monkeypatch.setattr(scheduler, "run_once", fake_run_once)

        client = TestClient(app)
        resp = client.post("/sources/poll", params={"force": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_ingested"] == 0
        assert isinstance(body["sources"], list)
