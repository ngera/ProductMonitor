"""Fetch stage (DESIGN.md §4.3).

For each enabled source/stream: load cursor, fetch, dedup against seen_ids,
append raw JSONL (system of record), update cursor. Errors are isolated
per-stream — one stream failing doesn't stop the run.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import structlog

from pipeline import storage
from pipeline.config import app_config, resolve_path, sources_config
from pipeline.util import append_jsonl, week_id_for
from sources import get_source
from sources.base import FetchStats, SourceCursor

log = structlog.get_logger()


def _raw_path(raw_root: Path, source: str, week_id: str, stream: str) -> Path:
    return raw_root / source / week_id / f"{stream}.jsonl"


def run_fetch(week_id: str) -> dict[str, Any]:
    """Returns completeness + counters for the run record."""
    app = app_config()
    fetching = app.get("fetching", {})
    raw_root = resolve_path(app["paths"]["raw_root"])

    counters = {"fetched": 0, "deduped": 0}
    completeness: dict[str, Any] = {"ceiling_hits": [], "comment_cap_hits": []}
    errors: list[str] = []

    for src in sources_config().get("sources", []):
        source_type = src["type"]
        try:
            source = get_source(source_type)
        except Exception as e:  # connector unavailable (e.g. praw/creds missing)
            errors.append(f"source {src['id']} init failed: {e}")
            log.error("source_init_failed", source=src["id"], error=str(e))
            continue

        for stream in src.get("streams", []):
            stream_name = stream.get("subreddit") or stream.get("id") or "default"
            cfg = {**fetching, **stream}
            cursor_ts = storage.get_cursor(source_type, stream_name)
            cursor = SourceCursor(cursor_ts=cursor_ts)
            stats = FetchStats()

            try:
                batch: list[dict[str, Any]] = []
                fresh_ids: list[str] = []
                for raw_item in source.fetch_since(cursor, cfg, stats):
                    unseen = storage.filter_unseen(source_type, [raw_item.external_id])
                    if not unseen:
                        counters["deduped"] += 1
                        continue
                    fresh_ids.append(raw_item.external_id)
                    batch.append(_serialize(raw_item, week_id))

                if batch:
                    path = _raw_path(raw_root, source_type, week_id, stream_name)
                    append_jsonl(path, batch)
                    storage.mark_seen(source_type, fresh_ids)
                    counters["fetched"] += len(batch)

                if cursor.cursor_ts is not None:
                    storage.set_cursor(source_type, stream_name, cursor.cursor_ts)

                for ch in stats.ceiling_hits:
                    completeness["ceiling_hits"].append(list(ch))
                for cc in stats.comment_cap_hits:
                    completeness["comment_cap_hits"].append(list(cc))

                log.info(
                    "stream_fetched", stream=stream_name, fetched=len(batch),
                    ceiling_hits=len(stats.ceiling_hits),
                )
            except Exception as e:  # per-stream isolation (§4.3 step 5)
                errors.append(f"stream {stream_name} failed: {e}")
                log.error("stream_failed", stream=stream_name, error=str(e))

            time.sleep(fetching.get("sleep_between_streams_seconds", 0))

    return {"counters": counters, "completeness": completeness, "errors": errors}


def _serialize(raw_item, week_id: str) -> dict[str, Any]:
    # Use the item's own created_at week if it differs (rare across week boundary).
    _ = week_id_for  # week assignment happens at normalize from created_at
    return {
        "source": raw_item.source,
        "source_display_name": raw_item.source_display_name,
        "external_id": raw_item.external_id,
        "url": raw_item.url,
        "parent_external_id": raw_item.parent_external_id,
        "author": raw_item.author,
        "created_at": raw_item.created_at.isoformat(),
        "title": raw_item.title,
        "body": raw_item.body,
        "engagement": raw_item.engagement,
        "raw": raw_item.raw,
    }
