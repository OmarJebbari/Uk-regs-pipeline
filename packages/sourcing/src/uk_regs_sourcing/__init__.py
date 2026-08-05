"""Source monitoring & auto-ingestion for the UK Regs pipeline.

This package watches authoritative sources (e.g. gov.uk planning gateway) for
new/updated Approved Documents, fetches changed PDFs, and feeds them into the
existing ingest pipeline — so the corpus stays current without manual dropping
of files.

Public surface:
    SourceRegistry, SourceEntry      — declarative source configuration
    FetchResult, SourceState         — runtime state
    BaseFetcher, HttpFetcher, LocalFetcher — pluggable fetchers
    StateStore, InMemoryStateStore   — seen-document tracking
    SourceMonitor                    — the orchestrator (diff → ingest)
    run_once, Scheduler              — one-shot + scheduled execution
"""

from uk_regs_sourcing.fetchers import (
    BaseFetcher,
    FetchResult,
    HttpFetcher,
    LocalFetcher,
    MockFetcher,
)
from uk_regs_sourcing.models import SourceEntry, SourceRegistry, SourceState
from uk_regs_sourcing.monitor import MonitorResult, SourceMonitor
from uk_regs_sourcing.registry import load_registry
from uk_regs_sourcing.scheduler import run_once
from uk_regs_sourcing.state import InMemoryStateStore, StateStore, get_state_store

__all__ = [
    "BaseFetcher",
    "FetchResult",
    "HttpFetcher",
    "InMemoryStateStore",
    "LocalFetcher",
    "MockFetcher",
    "MonitorResult",
    "SourceEntry",
    "SourceMonitor",
    "SourceRegistry",
    "SourceState",
    "StateStore",
    "get_state_store",
    "load_registry",
    "run_once",
]

__version__ = "0.1.0"
