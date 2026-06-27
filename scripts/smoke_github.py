"""Smoke test for the GitHub Issues source plugin.

Requires GITHUB_TOKEN in .env. Reads the most recent open issues from
a small Microsoft repo and prints titles + comment counts.

Usage:
    python scripts/smoke_github.py [owner/repo]   # default: microsoft/PowerToys
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from sources.base import FetchStats, SourceCursor
from sources.github_issues import GitHubIssuesSource


def main() -> int:
    load_dotenv()
    repo = sys.argv[1] if len(sys.argv) > 1 else "microsoft/PowerToys"
    src = GitHubIssuesSource()
    stats = FetchStats()
    cursor = SourceCursor()
    cfg = {
        "name": "smoke",
        "repos": [repo],
        "fetch_comments": False,        # keep smoke quick
        "per_page": 5,
    }
    # Hack: limit to 5 by short-circuiting at runtime — easiest is to break after N.
    print(f"GitHub issues from {repo}\n")
    n = 0
    for item in src.fetch_since(cursor, cfg, stats):
        n += 1
        eng = item.engagement or {}
        print(f"  [{eng.get('state','?'):>6}  {eng.get('comments',0):>3}c]  {item.title}")
        print(f"      labels: {eng.get('labels')}")
        print(f"      {item.url}")
        if n >= 5:
            break
    print(f"\n{n} items.  cursor advanced to: {cursor.cursor_ts}")
    return 0 if n > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
