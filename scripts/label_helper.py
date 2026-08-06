"""Interactive labeling CLI for the golden set (DESIGN.md §7.1).

Fetches a Reddit post/comment by URL, shows it, and walks you through the
labels, appending a JSONL line to eval/golden_set.jsonl. Shows per-class counts
so you can steer labeling toward under-filled cells (§7.1 sample floor).

    python scripts/label_helper.py https://reddit.com/r/Windows11/comments/...
    python scripts/label_helper.py --counts        # show class coverage only
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # Windows consoles default to cp1252; force UTF-8 for emoji/arrows.
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv  # noqa: E402

from pipeline.config import area_ids  # noqa: E402
from pipeline.models import CONTENT_TYPES  # noqa: E402

GOLDEN = Path(__file__).resolve().parent.parent / "eval" / "golden_set.jsonl"


def _existing() -> list[dict]:
    if not GOLDEN.exists():
        return []
    return [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]


def show_counts() -> None:
    area_c, ct_c = Counter(), Counter()
    for rec in _existing():
        labels = rec.get("labels", {})
        area_c.update(labels.get("areas", []))
        ct_c.update(labels.get("content_types", []))
    print(f"Golden set: {len(_existing())} items\n")
    print("Areas:")
    for a in area_ids():
        n = area_c.get(a, 0)
        flag = "  ⚠ under floor" if n < 8 else ""
        print(f"  {a:<14} {n}{flag}")
    print("\nContent types:")
    for c in sorted(CONTENT_TYPES):
        print(f"  {c:<16} {ct_c.get(c, 0)}")


def _prompt(text: str, default: str = "") -> str:
    val = input(f"{text} ").strip()
    return val or default


def _prompt_list(text: str, allowed: set[str] | None = None) -> list[str]:
    raw = input(f"{text} (comma-separated): ").strip()
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if allowed:
        bad = [x for x in items if x not in allowed]
        if bad:
            print(f"  ! ignoring unknown: {bad}")
            items = [x for x in items if x in allowed]
    return items


def label_url(url: str) -> None:
    import praw  # imported here so --counts works without praw

    load_dotenv()
    import os

    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ.get("REDDIT_USER_AGENT", "product-monitor/0.1"),
    )
    reddit.read_only = True

    if "/comments/" in url and url.rstrip("/").split("/")[-1] not in ("", ):
        submission = reddit.submission(url=url)
        title, body = submission.title, submission.selftext
        parent_context = None
    else:
        submission = reddit.submission(url=url)
        title, body = submission.title, submission.selftext
        parent_context = None

    print("\n" + "=" * 70)
    print(f"TITLE: {title}")
    print(f"BODY:\n{body[:1500]}")
    print("=" * 70 + "\n")

    print(f"Areas allowed: {', '.join(area_ids())}")
    areas = _prompt_list("areas", set(area_ids()))
    print(f"Content types allowed: {', '.join(sorted(CONTENT_TYPES))}")
    content_types = _prompt_list("content_types", CONTENT_TYPES)
    sentiment = float(_prompt("sentiment (-1..1):", "0") or 0)
    primary_area = _prompt(f"primary_area [{areas[0] if areas else 'other'}]:",
                           areas[0] if areas else "other")
    severity = _prompt("severity (critical/high/medium/low or blank):", "")
    windows_major = _prompt("windows_major (win10/win11/unknown):", "unknown")
    kb_numbers = _prompt_list("kb_numbers")

    rec = {
        "id": url,
        "title": title,
        "body": body,
        "source_display_name": f"r/{submission.subreddit.display_name}",
        "raw": {"parent_context": parent_context} if parent_context else {},
        "labels": {
            "areas": areas,
            "content_types": content_types,
            "sentiment": sentiment,
            "primary_area": primary_area,
            "severity": severity or None,
            "windows_major": windows_major,
            "kb_numbers": kb_numbers,
            "entities": [],  # add manually in the JSONL if needed
        },
    }
    with open(GOLDEN, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n[label_helper] appended. Golden set now {len(_existing())} items.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url", nargs="?", help="Reddit post/comment URL")
    ap.add_argument("--counts", action="store_true", help="show class coverage and exit")
    args = ap.parse_args()
    if args.counts or not args.url:
        show_counts()
        return 0
    label_url(args.url)
    show_counts()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
