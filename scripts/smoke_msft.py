"""Smoke test for the Microsoft Tech Community + Q&A RSS plugin.

Usage:
    python scripts/smoke_msft.py [feed_url]

Default: Windows Insiders Tech Community board RSS.
"""

from __future__ import annotations

import sys

from sources.base import FetchStats, SourceCursor
from sources.microsoft_community import MicrosoftCommunitySource

_DEFAULT_FEED = "https://techcommunity.microsoft.com/category/windowsinsiders/rss"


def main() -> int:
    feed_url = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_FEED
    src = MicrosoftCommunitySource()
    stats = FetchStats()
    cursor = SourceCursor()
    cfg = {"name": "smoke", "feed_url": feed_url, "display": "MS Community (smoke)"}
    print(f"Feed: {feed_url}\n")
    n = 0
    for item in src.fetch_since(cursor, cfg, stats):
        n += 1
        official = "[official]" if (item.raw or {}).get("is_official_voice") else "          "
        print(f"  {official} {item.created_at.strftime('%Y-%m-%d')}  {item.author or '<no author>'}")
        print(f"      {item.title}")
        print(f"      {item.url}")
        if n >= 5:
            break
    print(f"\n{n} items.  cursor advanced to: {cursor.cursor_ts}")
    if stats.ceiling_hits:
        print(f"warnings: {stats.ceiling_hits}")
    return 0 if n > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
