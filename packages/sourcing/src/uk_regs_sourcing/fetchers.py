"""
Pluggable source fetchers.

A fetcher is the adapter between an external source (HTTP directory, S3 bucket,
local folder) and the monitor. It exposes two operations:

    list_documents()  -> the documents the source currently advertises
    fetch(state)      -> the raw bytes for one document

This indirection makes the source set trivially extensible (gov.uk planning
portal, NHBC, local test fixtures…) and keeps the monitor pure & testable.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from uk_regs_core.ids import sha256_of

from uk_regs_sourcing.models import FetchResult, SourceEntry, SourceState

logger = logging.getLogger(__name__)


class BaseFetcher(ABC):
    """Abstract fetcher. One instance per source entry."""

    kind: str = "abstract"

    def __init__(self, entry: SourceEntry) -> None:
        self.entry = entry

    @abstractmethod
    def list_documents(self) -> list[SourceState]:
        """Return the documents this source currently advertises."""

    @abstractmethod
    def fetch(self, state: SourceState) -> FetchResult:
        """Download the bytes for one advertised document."""

    # ---- helpers -------------------------------------------------------- #
    def _state(
        self,
        *,
        document_id: str,
        title: str,
        version: str,
        url: str,
        sha256: str | None = None,
        size_bytes: int | None = None,
        published_at: datetime | None = None,
    ) -> SourceState:
        return SourceState(
            source_id=self.entry.source_id,
            document_id=document_id,
            title=title,
            version=version,
            url=url,
            sha256=sha256,
            size_bytes=size_bytes,
            published_at=published_at,
        )


# --------------------------------------------------------------------------- #
#  Local fetcher — reads a directory of PDFs (dev/test/on-prem share)
# --------------------------------------------------------------------------- #
class LocalFetcher(BaseFetcher):
    """Scans a local directory for *.pdf files."""

    kind = "local"

    def list_documents(self) -> list[SourceState]:
        path = self.entry.path
        if not path:
            return []
        root = Path(path)
        if not root.exists():
            logger.warning("local source '%s': path %s does not exist", self.entry.source_id, root)
            return []
        out: list[SourceState] = []
        for pdf in sorted(root.glob("*.pdf")):
            stat = pdf.stat()
            out.append(
                self._state(
                    document_id=pdf.stem,
                    title=pdf.stem.replace("_", " ").replace("-", " ").title(),
                    version=self.entry.default_version,
                    url=str(pdf.resolve()),
                    size_bytes=stat.st_size,
                    published_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                )
            )
        return out

    def fetch(self, state: SourceState) -> FetchResult:
        p = Path(urlparse(state.url).path) if "://" in state.url else Path(state.url)
        content = p.read_bytes()
        return FetchResult(
            document_id=state.document_id,
            version=state.version,
            title=state.title,
            url=state.url,
            content=content,
            sha256=sha256_of(content),
            source_id=self.entry.source_id,
            local_path=p,
        )


# --------------------------------------------------------------------------- #
#  SSRF guard — the http fetcher only talks to public, http(s) hosts
# --------------------------------------------------------------------------- #
class UnsafeUrlError(ValueError):
    """A source/manifest URL pointed somewhere the fetcher must not go."""


def _resolve_host(host: str) -> list[str]:
    """Resolve a hostname to its IP addresses. Isolated so tests can monkeypatch."""
    infos = socket.getaddrinfo(host, None)
    return [sockaddr[0] for *_head, sockaddr in infos]


def _validate_public_url(url: str, *, allow_private: bool) -> None:
    """
    Raise ``UnsafeUrlError`` unless ``url`` is an http(s) URL on a public host.

    Blocks non-http schemes (``file://``, ``ftp://``, …), loopback, RFC1918,
    link-local (incl. the ``169.254.169.254`` cloud-metadata endpoint),
    unique-local IPv6, unspecified and unresolved hosts. Called for the
    initial request URL **and for every redirect hop**.
    """
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError(f"scheme {parsed.scheme!r} not allowed for source fetch")
    host = parsed.hostname
    if not host:
        raise UnsafeUrlError("URL has no host")
    if allow_private:
        return
    # Literal IPs are checked directly; names are resolved and *every*
    # address must be public — a host with mixed public/private A/AAAA
    # records is refused.
    try:
        candidates = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            candidates = [ipaddress.ip_address(ip) for ip in _resolve_host(host)]
        except socket.gaierror as exc:
            raise UnsafeUrlError(f"cannot resolve source host {host!r}") from exc
    for ip in candidates:
        if not ip.is_global:
            raise UnsafeUrlError(f"source host {host!r} resolves to non-public IP {ip}")


# --------------------------------------------------------------------------- #
#  HTTP fetcher — pulls from a JSON manifest or an HTML directory listing
# --------------------------------------------------------------------------- #
class HttpFetcher(BaseFetcher):
    """
    Fetches documents over HTTP.

    Two modes (auto-detected from the base URL response Content-Type):

    1. **Manifest mode** (recommended): the source URL returns a JSON array:
           [{"document_id": "ad-b", "title": "Approved Document B",
             "version": "2026-ed1", "url": "https://.../ad-b-2026.pdf",
             "sha256": "...", "size_bytes": 12345, "published_at": "..."}]
       This is the most robust and is what we recommend publishers expose.

    2. **Scrape mode** (fallback): the source URL is an HTML directory listing;
       we extract <a href="*.pdf"> links. Versions default to the entry's
       ``default_version``.
    """

    kind = "http"

    def list_documents(self) -> list[SourceState]:

        url = self.entry.url
        if not url:
            return []
        resp = self._http_get(url, accept="application/json, text/html")
        ctype = resp.headers.get("content-type", "").lower()
        if "application/json" in ctype or url.endswith(".json"):
            return self._parse_manifest(resp.content)
        return self._scrape_html(resp.content, base=url)

    def fetch(self, state: SourceState) -> FetchResult:
        resp = self._http_get(state.url, accept="application/pdf")
        content = resp.content
        return FetchResult(
            document_id=state.document_id,
            version=state.version,
            title=state.title,
            url=state.url,
            content=content,
            sha256=sha256_of(content),
            source_id=self.entry.source_id,
        )

    # ---- internals ------------------------------------------------------ #
    def _http_get(self, url: str, *, accept: str) -> _Response:
        """
        Thin wrapper so tests can monkeypatch this without httpx.

        SSRF-hardened: the URL — and every redirect hop — is validated by
        ``_validate_public_url`` *before* a connection is made.
        """
        import httpx
        from uk_regs_core import get_settings

        allow_private = get_settings().sourcing_allow_private_hosts
        _validate_public_url(url, allow_private=allow_private)

        max_redirects = 5
        with httpx.Client(follow_redirects=False, timeout=60.0) as client:
            for _ in range(max_redirects + 1):
                resp = client.get(url, headers={"Accept": accept})
                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    url = urljoin(str(resp.url), location)
                    _validate_public_url(url, allow_private=allow_private)
                    continue
                resp.raise_for_status()
                return _Response(content=resp.content, headers=dict(resp.headers))
        raise UnsafeUrlError(f"too many redirects fetching {self.entry.url}")

    def _parse_manifest(self, body: bytes) -> list[SourceState]:
        import json

        data = json.loads(body)
        if not isinstance(data, list):
            raise ValueError(f"manifest at {self.entry.url} is not a JSON array")
        out: list[SourceState] = []
        for item in data:
            pub = item.get("published_at")
            published = datetime.fromisoformat(pub) if pub else None
            out.append(
                self._state(
                    document_id=item["document_id"],
                    title=item.get("title", item["document_id"]),
                    version=item.get("version", self.entry.default_version),
                    url=item["url"],
                    sha256=item.get("sha256"),
                    size_bytes=item.get("size_bytes"),
                    published_at=published,
                )
            )
        return out

    def _scrape_html(self, body: bytes, *, base: str) -> list[SourceState]:
        """Best-effort scrape of an HTML directory listing for *.pdf links."""
        import re

        html = body.decode("utf-8", errors="replace")
        hrefs = re.findall(r'href="([^"]+\.pdf)"', html, flags=re.IGNORECASE)
        out: list[SourceState] = []
        for href in hrefs:
            abs_url = urljoin(base, href)
            doc_id = Path(href).stem
            out.append(
                self._state(
                    document_id=doc_id,
                    title=doc_id.replace("_", " ").replace("-", " ").title(),
                    version=self.entry.default_version,
                    url=abs_url,
                )
            )
        return out


class _Response:
    """Internal carrier for an HTTP response (test-friendly)."""

    __slots__ = ("content", "headers")

    def __init__(self, content: bytes, headers: dict[str, str]) -> None:
        self.content = content
        self.headers = headers


# --------------------------------------------------------------------------- #
#  Mock fetcher — for tests and offline dev
# --------------------------------------------------------------------------- #
class MockFetcher(BaseFetcher):
    """Returns an in-memory set of documents. Never touches the network."""

    kind = "mock"

    # Class-level registry so tests can pre-seed document bytes across sources.
    _documents: dict[str, dict[str, bytes]] = {}

    @classmethod
    def seed(cls, source_id: str, documents: dict[str, bytes]) -> None:
        """Pre-seed document bytes for a source: {document_id: pdf_bytes}."""
        cls._documents[source_id] = dict(documents)

    @classmethod
    def reset(cls) -> None:
        cls._documents.clear()

    def list_documents(self) -> list[SourceState]:
        docs = self._documents.get(self.entry.source_id, {})
        out: list[SourceState] = []
        for doc_id, content in docs.items():
            out.append(
                self._state(
                    document_id=doc_id,
                    title=doc_id.replace("_", " ").replace("-", " ").title(),
                    version=self.entry.default_version,
                    url=f"mock://{self.entry.source_id}/{doc_id}",
                    sha256=sha256_of(content),
                    size_bytes=len(content),
                    published_at=datetime.now(UTC),
                )
            )
        return out

    def fetch(self, state: SourceState) -> FetchResult:
        content = self._documents[self.entry.source_id][state.document_id]
        return FetchResult(
            document_id=state.document_id,
            version=state.version,
            title=state.title,
            url=state.url,
            content=content,
            sha256=sha256_of(content),
            source_id=self.entry.source_id,
        )


# --------------------------------------------------------------------------- #
#  Factory
# --------------------------------------------------------------------------- #
def make_fetcher(entry: SourceEntry) -> BaseFetcher:
    """Construct the right fetcher for a source entry."""
    if entry.fetcher == "http":
        return HttpFetcher(entry)
    if entry.fetcher == "local":
        return LocalFetcher(entry)
    if entry.fetcher == "mock":
        return MockFetcher(entry)
    raise ValueError(f"unknown fetcher kind: {entry.fetcher!r}")
