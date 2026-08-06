"""Per-source engagement percentile (0-100) for the digest detail rows.

Pure Python — no numpy required. Percentile is within the same source's
distribution in the trailing 12 buckets (report_v2_design.md §5.4 chosen
over 'this run only' and 'all-time' for consistency with the chart window).

Sources without engagement metrics (some RSS/news) return None; template
renders those as `—`.
"""

from __future__ import annotations

import bisect
import json
from typing import Optional

from pipeline import storage


def _engagement_scalar(engagement_json: Optional[str]) -> Optional[int]:
    """Best-effort scalar from a source's engagement blob.

    Different sources use different keys (upvotes, likes, replies, views).
    We take the max numeric value — good enough for a within-source ranking
    without needing to know each source's semantics.
    """
    if not engagement_json:
        return None
    try:
        d = json.loads(engagement_json)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    vals = [v for v in d.values() if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not vals:
        return None
    return int(max(vals))


def distribution_by_source(week_ids: list[str]) -> dict[str, list[int]]:
    """{source: sorted ascending engagement values} across the week window.

    Sorted so `bisect_right(dist, x)` gives the count of values ≤ x, which
    directly yields the percentile rank.
    """
    if not week_ids:
        return {}
    ph = ",".join("?" * len(week_ids))
    rows = storage.query(
        f"SELECT source, engagement_json FROM items "
        f"WHERE is_relevant = TRUE AND week_id IN ({ph})",
        week_ids,
    )
    out: dict[str, list[int]] = {}
    for r in rows:
        e = _engagement_scalar(r.get("engagement_json"))
        if e is None:
            continue
        out.setdefault(r["source"], []).append(e)
    for k in out:
        out[k].sort()
    return out


def percentile(sorted_values: list[int], target: int) -> Optional[int]:
    """Percentile rank of `target` within `sorted_values` (0-100).

    Uses upper-bound insertion so ties round up. Returns None on empty input.
    """
    if not sorted_values:
        return None
    idx = bisect.bisect_right(sorted_values, target)
    return int(round(100 * idx / len(sorted_values)))


def trailing_week_ids(buckets: int = 12) -> list[str]:
    """Last N distinct week_ids present in `items`, oldest → newest."""
    rows = storage.query(
        "SELECT DISTINCT week_id FROM items ORDER BY week_id DESC LIMIT ?",
        [buckets],
    )
    return list(reversed([r["week_id"] for r in rows]))


def annotate_items(items: list[dict], dist: dict[str, list[int]]) -> None:
    """Fill `engagement_percentile` on each item dict in-place.

    Items whose source has no distribution (rare sources, or sources
    without engagement metrics at all) get None so the template renders
    them as `—`.
    """
    for it in items:
        e = _engagement_scalar(it.get("engagement_json"))
        if e is None:
            it["engagement_percentile"] = None
            continue
        sorted_vals = dist.get(it.get("source") or "") or []
        it["engagement_percentile"] = percentile(sorted_vals, e)
