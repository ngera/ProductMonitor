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

from pipeline import connections, storage
from pipeline.config import app_config, current_product, resolve_path, sources_config
from pipeline.util import append_jsonl, week_id_for
from sources import get_source
from sources.base import FetchStats, SourceCursor

log = structlog.get_logger()


def _raw_path(raw_root: Path, source: str, week_id: str, stream: str) -> Path:
    return raw_root / source / week_id / f"{stream}.jsonl"


def run_fetch(
    week_id: str,
    *,
    effective_since: float | None = None,
    effective_until: float | None = None,
    advance_cursor: bool = True,
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Returns completeness + counters for the run record.

    Time-window parameters (set by the orchestrator from CLI flags +
    persisted product time_range, via compute_effective_window):

      effective_since   epoch s, replaces the per-stream cursor as the floor
                        for this run. None = use the existing cursor as-is.
      effective_until   epoch s, post-filter: items with created_at >
                        effective_until are dropped before they hit JSONL.
      advance_cursor    if False, the per-stream cursor is NOT saved at the
                        end of this run. Used for historical backfills
                        (mode='range') so they don't move steady-state
                        incremental state.
      source_ids        if non-None, only sources whose `id` is in the list
                        are run. None = all configured sources.
    """
    app = app_config()
    fetching = app.get("fetching", {})
    # Per-product raw root: data/<product_id>/raw/.
    try:
        product_id = current_product().id
        raw_root = resolve_path(app["paths"]["data_root"]) / product_id / "raw"
    except Exception:
        product_id = ""
        raw_root = resolve_path(app["paths"]["raw_root"])

    # POST_V1_PLAN §4.9 — reset the shared ScrapeCreators client per run so
    # credits reset to zero and env-var changes (mock flag, cap) are re-read.
    # Cheap when SC isn't configured; safe when SC isn't installed.
    try:
        from sources.scrapecreators.client import reset_shared_client
        reset_shared_client(cap=int(fetching.get("scrapecreators_max_credits_per_run", 200)))
    except Exception:
        pass

    # Feature-flag gate for §4.9 SC sources. When off, we still allow the
    # plugins to register (so /connections shows them) but skip fetching.
    from pipeline import features as _feat
    scrapecreators_enabled = _feat.enabled("scrapecreators_enabled", product_id or None)
    scrapecreators_types = {
        "scrapecreators_reddit", "scrapecreators_x", "scrapecreators_tiktok",
    }

    counters = {"fetched": 0, "deduped": 0, "out_of_window": 0}
    completeness: dict[str, Any] = {"ceiling_hits": [], "comment_cap_hits": []}
    errors: list[str] = []

    allowed_ids: set[str] | None = set(source_ids) if source_ids else None
    # Fresh read per run — pausing via the webui takes effect on the next run
    # without needing a webui/pipeline restart.
    globally_paused: set[str] = connections.paused_types()

    for src in sources_config().get("sources", []):
        if allowed_ids is not None and src.get("id") not in allowed_ids:
            log.info("source_skipped_by_filter", source=src.get("id"))
            continue
        source_type = src["type"]

        # Pause precedence: connection-level pause supersedes product-level.
        # We check global first so the log line is unambiguous.
        if source_type in globally_paused:
            log.info(
                "source_paused_connection",
                source=src.get("id"), type=source_type,
                reason="global connection pause on /connections page",
            )
            continue
        if bool(src.get("paused")):
            log.info(
                "source_paused_product",
                source=src.get("id"), type=source_type,
                reason="paused for this product on the /sources page",
            )
            continue

        if source_type in scrapecreators_types and not scrapecreators_enabled:
            log.info(
                "source_skipped_feature_flag",
                source=src.get("id"), type=source_type,
                reason="features.scrapecreators_enabled is off",
            )
            continue

        try:
            source = get_source(source_type)
        except Exception as e:  # connector unavailable (e.g. praw/creds missing)
            errors.append(f"source {src['id']} init failed: {e}")
            log.error("source_init_failed", source=src["id"], error=str(e))
            continue

        for stream in src.get("streams", []):
            stream_name = (
                stream.get("name")
                or stream.get("subreddit")
                or stream.get("feed_url")
                or stream.get("id")
                or "default"
            )
            # Per-stream pause: pause r/Windows11 without pausing r/pcaudio.
            # Precedence order (already checked above): global connection > product-instance.
            # This is the third and finest granularity.
            if bool(stream.get("paused")):
                log.info(
                    "stream_paused",
                    source=src.get("id"), type=source_type, stream=stream_name,
                    reason="paused for this stream on the /sources page",
                )
                continue
            cfg = {**fetching, **stream}
            # Effective floor: explicit override wins over the persisted cursor.
            if effective_since is not None:
                floor_ts = effective_since
            else:
                floor_ts = storage.get_cursor(source_type, stream_name)
            cursor = SourceCursor(cursor_ts=floor_ts)
            stats = FetchStats()

            try:
                batch: list[dict[str, Any]] = []
                fresh_ids: list[str] = []
                for raw_item in source.fetch_since(cursor, cfg, stats):
                    # Upper bound: drop items past the window's right edge.
                    if effective_until is not None:
                        ts = raw_item.created_at.timestamp()
                        if ts > effective_until:
                            counters["out_of_window"] += 1
                            continue
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

                if advance_cursor and cursor.cursor_ts is not None:
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
