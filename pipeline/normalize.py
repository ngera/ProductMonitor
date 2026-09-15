"""Normalize stage (DESIGN.md §4.4.1).

Raw JSONL -> items table. Deterministic, idempotent (upsert by id).

Computes two derived structural fields on the way through:

- `is_reply`: True iff the raw item carries a parent_external_id (i.e. it's
  a comment / reply within a thread).
- `author_intent`: 'editorial' | 'user_original' | 'user_reply'.
  Lets downstream stages weight a Microsoft staff post differently from a
  Reddit user comment, etc. Single source of truth lives here so adding a
  new source means one entry in `_INTENT_RULES`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import structlog

from pipeline import storage
from pipeline.canonical_url import canonicalize as _canonicalize_url
from pipeline.config import app_config, current_product, resolve_path
from pipeline.util import read_jsonl, week_id_for

log = structlog.get_logger()


# --- author_intent rules ----------------------------------------------------


def _intent_for_microsoft_community(rec: dict[str, Any]) -> str:
    """Tech Community + Q&A: staff / MVP voices read as editorial; everyone
    else as user_original. The MS Community plugin tags is_official_voice
    via the author-string heuristic at fetch time."""
    if (rec.get("raw") or {}).get("is_official_voice"):
        return "editorial"
    return "user_original"


# Map source name -> function(rec) -> intent. Called only for top-level items
# (parent_external_id is None). Anything with a parent is always 'user_reply'.
_INTENT_RULES: dict[str, Callable[[dict[str, Any]], str]] = {
    "microsoft_community": _intent_for_microsoft_community,
    # Future RSS source (when added):
    # "rss": lambda rec: "editorial",
}


def _author_intent(rec: dict[str, Any]) -> str:
    if rec.get("parent_external_id"):
        return "user_reply"
    rule = _INTENT_RULES.get(rec.get("source") or "")
    if rule:
        return rule(rec)
    return "user_original"


# --- normalize loop ---------------------------------------------------------


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def run_normalize(week_id: str) -> dict[str, Any]:
    app = app_config()
    # Per-product raw root: data/<product_id>/raw/. Mirrors the fetch stage —
    # without this normalize reads ./data/raw (legacy single-product path)
    # while fetch is writing under ./data/<product>/raw, so every per-product
    # run produces 0 normalized items.
    try:
        product_id = current_product().id
        raw_root = resolve_path(app["paths"]["data_root"]) / product_id / "raw"
    except Exception:
        raw_root = resolve_path(app["paths"]["raw_root"])
    fetched_at = datetime.now().astimezone()

    rows: list[dict[str, Any]] = []
    week_dirs = list(raw_root.glob(f"*/{week_id}"))
    for wd in week_dirs:
        for jsonl in wd.glob("*.jsonl"):
            for rec in read_jsonl(jsonl):
                rows.append(_to_item_row(rec, jsonl, fetched_at))

    rows, dedup_dropped = _dedup_by_canonical_url(rows)

    n = storage.upsert_items(rows)
    log.info("normalized", items=n, dedup_dropped=dedup_dropped)
    return {"normalized": n, "dedup_dropped": dedup_dropped}


def _dedup_by_canonical_url(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Collapse rows that share a canonical_url (ADR-0024).

    Only top-level items (is_reply=False) participate. Replies are
    per-thread; a comment's URL may repeat across unrelated threads and
    we don't want to fold them together. Rows with a NULL canonical_url
    (malformed URLs, non-http schemes) also skip — they fall back to
    per-source identity, matching pre-ADR-0024 behavior.

    Winner selection when a group has >1 candidate:
      1. Earliest `created_at` — the "original" publication, ahead of
         syndicators like HN link posts or subreddit shares.
      2. Source name alphabetically — deterministic tiebreak on timestamp.
      3. external_id — final tiebreak for full determinism.

    Every drop is logged so operators can spot false positives. If false
    positives become a real problem in dogfooding, add a title-similarity
    second gate; not doing that upfront keeps the rule debuggable.
    """
    dedup_dropped = 0
    by_canonical: dict[str, list[dict[str, Any]]] = {}
    kept: list[dict[str, Any]] = []

    for row in rows:
        canonical = row.get("canonical_url")
        # Replies and NULL-canonical rows bypass dedup entirely.
        if not canonical or row.get("is_reply"):
            kept.append(row)
            continue
        by_canonical.setdefault(canonical, []).append(row)

    for canonical, group in by_canonical.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        group.sort(key=lambda r: (
            r.get("created_at"),
            r.get("source") or "",
            r.get("external_id") or "",
        ))
        winner = group[0]
        kept.append(winner)
        for loser in group[1:]:
            dedup_dropped += 1
            log.info(
                "dedup_dropped_canonical",
                canonical_url=canonical,
                kept=winner["id"],
                dropped=loser["id"],
                dropped_source=loser.get("source"),
            )

    return kept, dedup_dropped


def _to_item_row(rec: dict[str, Any], raw_path: Path, fetched_at: datetime) -> dict[str, Any]:
    created = _parse_dt(rec["created_at"])
    item_id = f"{rec['source']}:{rec['external_id']}"
    parent_id = (
        f"{rec['source']}:{rec['parent_external_id']}"
        if rec.get("parent_external_id")
        else None
    )
    return {
        "id": item_id,
        "source": rec["source"],
        "source_display_name": rec["source_display_name"],
        "external_id": rec["external_id"],
        "url": rec["url"],
        "parent_id": parent_id,
        "author": rec.get("author"),
        "created_at": created,
        "fetched_at": fetched_at,
        "week_id": week_id_for(created),
        "title": rec.get("title"),
        "body": rec.get("body") or "",
        "engagement_json": json.dumps(rec.get("engagement", {})),
        "raw_ref": str(raw_path),
        "filter_status": None,
        "relevance_score": None,
        "is_relevant": None,
        "is_reply": bool(rec.get("parent_external_id")),
        "author_intent": _author_intent(rec),
        "canonical_url": _canonicalize_url(rec.get("url")),
    }
