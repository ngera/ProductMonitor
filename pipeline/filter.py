"""Filter stage A — heuristic only, no LLM (DESIGN.md §4.4).

Conservative: we'd rather over-include and let the relevance gate (§4.5) decide.
Every drop is recorded as filter_status='dropped:<reason>' so nothing vanishes silently.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import structlog

from pipeline import storage
from pipeline.config import app_config
from pipeline.util import hamming, simhash

log = structlog.get_logger()

# Watchlist: items matching these survive even below the engagement threshold.
WATCHLIST_RE = re.compile(r"(KB\d{7}|CVE-\d{4}-\d+|\b\d{5}\.\d+\b)", re.IGNORECASE)

DEAD_BODIES = {"[deleted]", "[removed]", ""}


def run_filter(week_id: str) -> dict[str, Any]:
    app = app_config()
    fcfg = app.get("filter", {})
    min_chars = fcfg.get("min_body_chars", 50)
    threshold = app.get("fetching", {}).get("default_engagement_threshold", 5)
    ham_thresh = app.get("grouping", {}).get("simhash_hamming_threshold", 4)

    items = storage.items_for_week(week_id)
    counters = {"passed": 0, "dropped": 0}
    drop_reasons: dict[str, int] = {}

    seen_urls: set[str] = set()
    title_hashes: list[int] = []

    # Process posts before comments so a post's URL dedups its echoes.
    items.sort(key=lambda i: (i.get("parent_id") is not None, i["created_at"]))

    for it in items:
        reason = _drop_reason(it, min_chars, threshold, seen_urls, title_hashes, ham_thresh)
        if reason:
            status = f"dropped:{reason}"
            counters["dropped"] += 1
            drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
        else:
            status = "passed"
            counters["passed"] += 1
            seen_urls.add(_canonical_url(it["url"]))
            if it.get("title"):
                title_hashes.append(simhash(it["title"]))
        storage.set_filter_status(it["id"], status)

    log.info("filtered", **counters, reasons=drop_reasons)
    return {"counters": counters, "drop_reasons": drop_reasons}


def _drop_reason(
    it: dict[str, Any],
    min_chars: int,
    threshold: int,
    seen_urls: set[str],
    title_hashes: list[int],
    ham_thresh: int,
) -> str | None:
    body = (it.get("body") or "").strip()
    title = (it.get("title") or "").strip()

    if body in DEAD_BODIES and not title:
        return "deleted_or_empty"

    text = f"{title} {body}"
    on_watchlist = bool(WATCHLIST_RE.search(text))

    if len(body) < min_chars and len(title) < 20 and not on_watchlist:
        return "too_short"

    canon = _canonical_url(it["url"])
    if canon in seen_urls:
        return "duplicate_url"

    if title:
        h = simhash(title)
        for existing in title_hashes:
            if hamming(h, existing) <= ham_thresh:
                return "duplicate_title"

    if not on_watchlist:
        eng = _engagement(it)
        if eng["upvotes"] < threshold and eng["comment_count"] < threshold:
            return "low_engagement"

    return None


def _engagement(it: dict[str, Any]) -> dict[str, int]:
    try:
        e = json.loads(it.get("engagement_json") or "{}")
    except json.JSONDecodeError:
        e = {}
    return {
        "upvotes": int(e.get("upvotes", 0)),
        "comment_count": int(e.get("comment_count", 0)),
    }


_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_place", "utm_pubreferrer",
    "fbclid", "gclid", "yclid", "mc_cid", "mc_eid", "msclkid",
    "_ga", "_gl", "igshid", "cmp", "ref", "ref_src", "ref_url",
})


def _canonical_url(url: str) -> str:
    """Canonicalize a URL for dedup. Strips only known tracking params —
    identity-carrying params like ?id=… (HN, YouTube, MS Community) are
    preserved. Without this, every HN item collapsed to `.../item` and only
    the first one survived (issue observed 2026-07: 63 legit items dropped
    as duplicate_url).
    """
    if not url:
        return ""
    try:
        p = urlsplit(url.strip().lower())
        kept = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                if k not in _TRACKING_PARAMS]
        # Sort for stability so ?a=1&b=2 and ?b=2&a=1 collide.
        query = urlencode(sorted(kept))
        path = p.path.rstrip("/")
        return urlunsplit((p.scheme, p.netloc, path, query, ""))
    except Exception:
        # If anything is genuinely malformed, fall back to lowercase-strip.
        return url.strip().lower().rstrip("/")
