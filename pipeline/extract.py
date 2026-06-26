"""Regex entity pre-pass (DESIGN.md §4.7 step 1).

Pure & deterministic. Produces hints fed to the LLM and a `regex_extractions`
record stored independently for traceability.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from pipeline.config import vendors_config

KB_RE = re.compile(r"KB\d{7}", re.IGNORECASE)
CVE_RE = re.compile(r"CVE-\d{4}-\d+", re.IGNORECASE)
BUILD_RE = re.compile(r"\b\d{5}\.\d+\b")


@dataclass
class RegexExtractions:
    kb_numbers: list[str] = field(default_factory=list)
    cve_ids: list[str] = field(default_factory=list)
    build_numbers: list[str] = field(default_factory=list)
    vendor_hits: list[str] = field(default_factory=list)


@lru_cache(maxsize=1)
def _vendor_patterns() -> list[tuple[str, re.Pattern]]:
    """Compile (canonical, pattern) for each active vendor incl. aliases."""
    out: list[tuple[str, re.Pattern]] = []
    for v in vendors_config().get("vendors", []):
        if not v.get("active", True):
            continue
        names = [v["canonical"], *v.get("aliases", [])]
        # word-boundary, case-insensitive, longest names first
        escaped = sorted((re.escape(n) for n in names), key=len, reverse=True)
        pat = re.compile(r"\b(" + "|".join(escaped) + r")\b", re.IGNORECASE)
        out.append((v["canonical"], pat))
    return out


def extract(text: str) -> RegexExtractions:
    text = text or ""
    res = RegexExtractions(
        kb_numbers=_dedup_upper(KB_RE.findall(text)),
        cve_ids=_dedup_upper(CVE_RE.findall(text)),
        build_numbers=_dedup(BUILD_RE.findall(text)),
    )
    hits: list[str] = []
    for canonical, pat in _vendor_patterns():
        if pat.search(text):
            hits.append(canonical)
    res.vendor_hits = _dedup(hits)
    return res


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
