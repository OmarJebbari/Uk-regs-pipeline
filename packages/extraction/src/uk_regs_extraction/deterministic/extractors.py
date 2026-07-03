"""
Deterministic extractors.

These run BEFORE the LLM. They pull precise, regex-able facts:
  - dimensions and measurements (mm, m, m^2, etc.)
  - standards references (BS EN, BS, EN ISO, etc.)
  - section paths (2.3.1, B1, etc.)

The LLM then interprets the prose; these give it typed anchors so it cannot
misread "1,200 mm" as "1200 m" or hallucinate a standard reference.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- Measurements -----------------------------------------------------------
_MEASUREMENT = re.compile(
    r"""
    (?P<value>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)
    \s*
    (?P<unit>mm|cm|m|km|mm2|m2|cm2|degC|degF|kN|kN/m2|kN/m|N/mm2|MPa|GPa|dB|lux|lx|W/m2|W|m3|m3/h|ltr|L|kg|kW|kWh|MJ)
    \b
    """,
    re.VERBOSE | re.IGNORECASE,
)

# --- Standards references ---------------------------------------------------
_STANDARDS = re.compile(
    r"""
    \b
    (?P<prefix>BS\s*EN|BS|EN\s*ISO|EN|PD|ISO|NA\s*to)
    \s+
    (?P<number>\d[\dA-Z\-]*(?::\d{4})?)
    \b
    """,
    re.VERBOSE,
)

# --- Section paths ----------------------------------------------------------
_SECTION = re.compile(
    r"""
    \b
    (?:
        (?:Section\s+)?
        (?P<num>\d+(?:\.\d+){0,4}(?:\.[a-z])?)
        |
        (?P<letter>[A-Z]\d{1,2}(?:\.\d+)?)
        |
        Appendix\s+(?P<app>[A-Z0-9]+)
    )
    \b
    """,
    re.VERBOSE,
)


@dataclass(slots=True)
class DeterministicFacts:
    """Structured facts pulled by regex from a text block."""

    measurements: list[dict] = field(default_factory=list)
    standards: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None

    @property
    def has_any(self) -> bool:
        return bool(self.measurements or self.standards or self.sections)

    def as_parameters(self) -> dict:
        params: dict[str, float | str] = {}
        for m in self.measurements:
            key = f"measurement_{m['unit'].lower()}"
            try:
                params[key] = float(str(m["value"]).replace(",", ""))
            except ValueError:
                params[key] = m["value"]
        return params


def extract_facts(text: str) -> DeterministicFacts:
    """Extract all deterministic facts from a text block."""
    measurements = [
        {"value": m.group("value"), "unit": m.group("unit")}
        for m in _MEASUREMENT.finditer(text)
    ]
    standards = [re.sub(r"\s+", " ", s) for s in (m.group(0).strip() for m in _STANDARDS.finditer(text))]
    sections: list[str] = []
    for m in _SECTION.finditer(text):
        sec = m.group("num") or m.group("letter") or m.group("app")
        if sec:
            sections.append(sec)
    return DeterministicFacts(measurements=measurements, standards=standards, sections=sections)


def normalize_standard(ref: str) -> str:
    """Canonical form: 'BS EN 1991-1-1' from 'BS  EN 1991-1-1:2002+A1:2015'."""
    ref = re.sub(r"\s+", " ", ref.strip())
    return re.split(r":", ref)[0].strip()


def normalize_measurement(value: str) -> float:
    """Parse a measurement value string to float (handles thousands separators)."""
    return float(value.replace(",", ""))