"""Which sources came back empty this week, and why — for the digest banner.

The fetch stage records stream health in `completeness.ceiling_hits` as
`("rss:<stream>:<reason>", 0.0)` (sources/rss.py). Until now that only
reached the run log, so a digest built while Reddit was rate-limiting looked
like a quiet week. This turns those entries into a short statement of
consequence for the top of the digest.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

# reason → (what happened, what it means for this digest)
_REASONS: dict[str, tuple[str, str]] = {
    "rate-limited": (
        "rate-limited (HTTP 429)",
        "posts from these streams are missing this week; the next run retries "
        "after the cooldown",
    ),
    "dead": (
        "feed no longer exists (HTTP 404/410)",
        "remove or replace these streams in Sources",
    ),
    "http": ("returned an HTTP error", "posts from these streams are missing this week"),
    "network": ("could not be reached", "posts from these streams are missing this week"),
    "unparseable": ("returned a feed that could not be read",
                    "check the feed URL in Sources"),
}


def _stream_labels(sources: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Stream name → what the reader knows it as (r/<sub>, feed display)."""
    out: dict[str, str] = {}
    for src in sources or []:
        for st in src.get("streams") or []:
            name = st.get("name")
            if not name:
                continue
            if st.get("display"):
                out[name] = st["display"]
            elif st.get("subreddit"):
                sub = str(st["subreddit"]).strip().lstrip("/")
                out[name] = sub if sub.lower().startswith("r/") else "r/" + sub
            else:
                out[name] = name
    return out


def fetch_gaps(completeness: Optional[dict[str, Any]],
               sources: Iterable[dict[str, Any]] = ()) -> list[dict[str, Any]]:
    """Group failed streams by reason.

    Returns `[{"reason", "what", "consequence", "streams": [label, ...]}]`,
    rate limits first. Empty when every stream fetched.
    """
    labels = _stream_labels(sources)
    grouped: dict[str, list[str]] = {}
    for hit in (completeness or {}).get("ceiling_hits") or []:
        key = hit[0] if isinstance(hit, (list, tuple)) and hit else hit
        if not isinstance(key, str) or not key.startswith("rss:"):
            continue
        _, _, rest = key.partition(":")
        stream, _, reason = rest.rpartition(":")
        if not stream or reason not in _REASONS:
            continue
        label = labels.get(stream, stream)
        bucket = grouped.setdefault(reason, [])
        if label not in bucket:
            bucket.append(label)

    order = list(_REASONS)
    return [
        {
            "reason": reason,
            "what": _REASONS[reason][0],
            "consequence": _REASONS[reason][1],
            "streams": sorted(streams),
        }
        for reason, streams in sorted(grouped.items(), key=lambda kv: order.index(kv[0]))
    ]


def completeness_for_run(run_id: str) -> Optional[dict[str, Any]]:
    """The run's stored completeness, for re-renders that weren't handed it."""
    from pipeline import storage
    try:
        rows = storage.query("SELECT completeness FROM runs WHERE run_id = ?", [run_id])
    except Exception:
        return None
    if not rows or not rows[0].get("completeness"):
        return None
    raw = rows[0]["completeness"]
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return None
