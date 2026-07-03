"""
Neo4j driver singleton.

Lazy-initialised, thread-safe. Use `get_driver()`; close on shutdown.
"""

from __future__ import annotations

import threading

from neo4j import Driver, GraphDatabase

from uk_regs_core import ConfigError, Settings, get_settings

_driver: Driver | None = None
_lock = threading.Lock()


def get_driver(settings: Settings | None = None) -> Driver:
    global _driver
    if _driver is not None:
        return _driver
    with _lock:
        if _driver is not None:
            return _driver
        s = settings or get_settings()
        _driver = GraphDatabase.driver(
            s.neo4j_uri,
            auth=(s.neo4j_user, s.neo4j_password),
        )
    return _driver


def close_driver() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None