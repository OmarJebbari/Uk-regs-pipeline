"""Typed domain exceptions."""

from __future__ import annotations


class UkRegsError(Exception):
    """Base for all domain errors."""


class ExtractionError(UkRegsError):
    """Failed to parse or extract from a PDF."""


class ReconciliationError(UkRegsError):
    """Reconciliation/classification failed."""


class StorageError(UkRegsError):
    """Object-store / DB / graph write failure."""


class UngroundedError(UkRegsError):
    """An answer was requested but no citation could be found.

    In compliance contexts, returning 'not found' is safer than guessing.
    """


class ChangeSetRejected(UkRegsError):
    """A change-set was rejected by review and must not be applied."""


class ConfigError(UkRegsError):
    """Missing or invalid configuration."""
