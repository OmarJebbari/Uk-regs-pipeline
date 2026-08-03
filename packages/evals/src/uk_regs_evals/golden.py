"""
Loaders for the golden datasets that back the eval suite.

A golden dataset is a YAML file in ``evals/golden/``. Each file declares a
``suite`` (retrieval | reconciliation | extraction) and a list of ``cases``.
This module turns the raw YAML into typed dataclasses so the harnesses can
iterate over them safely.

Typical usage::

    from uk_regs_evals.golden import load_cases, RetrievalCase

    cases = load_cases("retrieval")
    for c in cases:
        assert isinstance(c, RetrievalCase)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml

SuiteName = Literal["retrieval", "reconciliation", "extraction"]

# The golden YAML files live at the *project* root: ``<project>/evals/golden/``.
# In this monorepo the package is nested under ``packages/evals/src/...``, so we
# walk up from this file until we find a directory containing ``evals/golden``.
# This is robust to both editable installs (``uv sync``) and packaged wheels.


def _find_golden_dir() -> Path:
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "evals" / "golden"
        if candidate.is_dir():
            return candidate
    # Fallback: best-guess conventional location (project root = parents[4]).
    return here.parents[4] / "evals" / "golden"


_GOLDEN_DIR = _find_golden_dir()

T = TypeVar("T")


# --------------------------------------------------------------------------- #
#  Case dataclasses
# --------------------------------------------------------------------------- #
@dataclass(slots=True, frozen=True)
class RetrievalCase:
    id: str
    query: str
    expected_rule_ids: list[str]
    expected_category: str | None
    note: str = ""


@dataclass(slots=True, frozen=True)
class ReconciliationCase:
    id: str
    description: str
    new_rule: dict[str, Any]
    corpus_rule: dict[str, Any] | None
    expected_decision: str


@dataclass(slots=True, frozen=True)
class ExtractionExpected:
    measurements: list[dict[str, Any]] = field(default_factory=list)
    standards: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    parameter_keys: list[str] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class ExtractionCase:
    id: str
    text: str
    expected: ExtractionExpected


# --------------------------------------------------------------------------- #
#  Loader
# --------------------------------------------------------------------------- #
def golden_dir() -> Path:
    """Return the path to the golden dataset directory."""
    return _GOLDEN_DIR


def _read_yaml(suite: SuiteName) -> dict[str, Any]:
    path = _GOLDEN_DIR / f"{suite}.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"Golden dataset not found: {path}. "
            f"Create it under evals/golden/{suite}.yaml."
        )
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping at the top level.")
    return data


_VALID_SUITES = {"retrieval", "reconciliation", "extraction"}


def load_cases(suite: SuiteName) -> list[Any]:
    """
    Load all cases for a suite and return them as typed dataclasses.

    The return type is a union of the three case types; callers typically know
    which suite they asked for and can rely on ``isinstance`` if needed.
    """
    if suite not in _VALID_SUITES:
        raise ValueError(
            f"Unknown suite: {suite!r}. "
            f"Expected one of: {', '.join(sorted(_VALID_SUITES))}."
        )
    data = _read_yaml(suite)
    raw_cases = data.get("cases", [])
    if not isinstance(raw_cases, list):
        raise ValueError(f"{suite}: 'cases' must be a list.")

    if suite == "retrieval":
        return [
            RetrievalCase(
                id=str(c["id"]),
                query=str(c["query"]),
                expected_rule_ids=list(c.get("expected_rule_ids", [])),
                expected_category=c.get("expected_category"),
                note=str(c.get("note", "")),
            )
            for c in raw_cases
        ]

    if suite == "reconciliation":
        return [
            ReconciliationCase(
                id=str(c["id"]),
                description=str(c.get("description", "")),
                new_rule=dict(c["new_rule"]),
                corpus_rule=dict(c["corpus_rule"]) if c.get("corpus_rule") else None,
                expected_decision=str(c["expected_decision"]),
            )
            for c in raw_cases
        ]

    if suite == "extraction":
        out: list[ExtractionCase] = []
        for c in raw_cases:
            exp = c.get("expected", {})
            out.append(
                ExtractionCase(
                    id=str(c["id"]),
                    text=str(c["text"]),
                    expected=ExtractionExpected(
                        measurements=list(exp.get("measurements", [])),
                        standards=list(exp.get("standards", [])),
                        sections=list(exp.get("sections", [])),
                        parameter_keys=list(exp.get("parameter_keys", [])),
                    ),
                )
            )
        return out

    raise ValueError(f"Unknown suite: {suite!r}")