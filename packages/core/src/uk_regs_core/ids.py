"""Deterministic, readable ID generators for entities."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def slug(text: str, max_len: int = 24) -> str:
    """Uppercase alphanumeric slug, safe for IDs."""
    s = _NON_ALNUM.sub("_", text.upper().strip())
    return s.strip("_")[:max_len]


def doc_id_from_title(title: str) -> str:
    """e.g. 'Approved Document B' -> 'APPROVED_DOCUMENT_B'."""
    return slug(title, max_len=40)


def rule_id(doc_id: str, section_path: str, *, index: int = 0) -> str:
    """
    Stable rule ID, e.g. 'AD-B__2_2_3_B__0'.

    Deterministic from (doc, section, index) so re-running extraction
    on the same PDF yields the same ID.
    """
    sec = section_path.replace("/", "_").strip("_").upper()
    return f"{doc_id}__{sec}__{index}"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ulid_like() -> str:
    """Compact unique id (timestamp-prefixed, sortable)."""
    import time

    ts = format(int(time.time() * 1000), "013x")
    import secrets

    rand = secrets.token_hex(8)
    return f"{ts}{rand}"


def change_set_id(doc_id: str) -> str:
    return f"CS_{doc_id}_{ulid_like()[:12]}"