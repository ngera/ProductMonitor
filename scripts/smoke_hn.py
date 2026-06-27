"""Smoke test for the Hacker News source plugin.

Usage:
    python scripts/smoke_hn.py [query]    # default: "windows 11"
"""

from __future__ import annotations

import sys

from sources.base import FetchStats, SourceCursor
from sources.hn import HackerNewsSource


def main() -> int:
    query = sys.argv[1] if len(sys.argv) > 1 else "windows 11"
    src = HackerNewsSource()
    stats = FetchStats()
    cursor = SourceCursor()
    cfg = {
        "name": "smoke",
        "search_queries": [query],
        "include_tags": ["story"],
        "max_pages_per_query": 1,
        "hits_per_page": 5,
    }
    print(f"HN search: {query!r}\n")
    n = 0
    for item in src.fetch_since(cursor, cfg, stats):
        n += 1
        score = (item.engagement or {}).get("points") or 0
        comments = (item.engagement or {}).get("comment_count") or 0
        print(f"  [{score:>4}pts {comments:>3}c]  {item.title or '<comment>'}")
        print(f"      {item.url}")
    print(f"\n{n} items.  cursor advanced to: {cursor.cursor_ts}")
    if stats.ceiling_hits:
        print(f"ceiling hits: {stats.ceiling_hits}")
    return 0 if n > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
