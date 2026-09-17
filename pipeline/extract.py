"""Regex entity pre-pass (DESIGN.md §4.7 step 1).

Pure & deterministic. Produces hints fed to the LLM and a `regex_extractions`
record stored independently for traceability.

KB / Microsoft patch IDs were removed from this path (ADR-0029) — they are
not a product-agnostic grouping signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CVE_RE = re.compile(r"CVE-\d{4}-\d+", re.IGNORECASE)
BUILD_RE = re.compile(r"\b\d{5}\.\d+\b")


@dataclass
class RegexExtractions:
    cve_ids: list[str] = field(default_factory=list)
    build_numbers: list[str] = field(default_factory=list)


def extract(text: str) -> RegexExtractions:
    text = text or ""
    return RegexExtractions(
        cve_ids=_dedup_upper(CVE_RE.findall(text)),
        build_numbers=_dedup(BUILD_RE.findall(text)),
    )


def _dedup(xs: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _dedup_upper(xs: list[str]) -> list[str]:
    return _dedup([x.upper() for x in xs])
