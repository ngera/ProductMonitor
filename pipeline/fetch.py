"""Fetch stage (DESIGN.md §4.3).

For each enabled source/stream: load cursor, fetch, dedup against seen_ids,
append raw JSONL (system of record), update cursor. Errors are isolated
per-stream — one stream failing doesn't stop the run.

Execution model (ADR-0023):
- Serial by default (backwards compatible).
- Concurrent via `fetch_concurrency_enabled` flag: ThreadPoolExecutor
  dispatches (src, stream) work items, with two semaphores bounding
  in-flight work (global + per-host). Rate limits are per-provider so
  the per-host cap is what actually protects Reddit/HN from us.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import structlog

from pipeline import connections, storage
from pipeline.config import app_config, current_product, resolve_path, sources_config
from pipeline.util import append_jsonl, week_id_for
from sources import get_source
from sources.base import FetchStats, SourceCursor

log = structlog.get_logger()


def _raw_path(raw_root: Path, source: str, week_id: str, stream: str) -> Path:
    return raw_root / source / week_id / f"{stream}.jsonl"


# ---------------------------------------------------------------------------
# Concurrent-fetch scaffolding (ADR-0023)
# ---------------------------------------------------------------------------
#
# Threaded, not asyncio: every source connector uses sync httpx/praw/feedparser
# and an async retrofit is a separate project. Threads let us parallelize the
# network wait without touching source implementations.
#
# Per-host cap is what actually protects providers from us — Reddit rate-limits
# by IP+UA regardless of how many streams we run against it. The global cap
# just keeps total in-flight sanity-bounded.


@dataclass
class _StreamResult:
    """What one worker returns. All fields are per-stream; the reducer
    below folds these into the run-level counters/errors/completeness."""
    fetched: int = 0
    deduped: int = 0
    out_of_window: int = 0
    ceiling_hits: list[list[Any]] = field(default_factory=list)
    comment_cap_hits: list[list[Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _host_key(source_type: str, stream: dict[str, Any]) -> str:
    """Bucket per rate-limit domain. For URL-based sources use the host;
    for API-based sources (praw, github_issues) fall back to source type
    so both count against the same provider quota."""
    for candidate in (stream.get("feed_url"), stream.get("url")):
        if candidate:
            from urllib.parse import urlsplit
            try:
                host = urlsplit(str(candidate)).hostname
                if host:
                    return host.lower()
            except ValueError:
                pass
    return source_type


def _dispatch(
    work_items: list[tuple[Callable[[], _StreamResult], str]],
    *,
    max_workers: int,
    per_host_cap: int,
) -> list[_StreamResult]:
    """Run `work_items` (a list of (callable, host_key) tuples) through a
    ThreadPoolExecutor with a per-host semaphore. Each worker acquires
    its host's semaphore before running and releases after.

    Returns results in submission order — a per-stream failure surfaces
    as `_StreamResult(errors=[str(e)])` rather than a raised exception,
    so per-stream isolation is preserved regardless of executor state.
    """
    host_semaphores: dict[str, threading.Semaphore] = {}
    host_lock = threading.Lock()

    def _sem_for(host: str) -> threading.Semaphore:
        with host_lock:
            sem = host_semaphores.get(host)
            if sem is None:
                sem = threading.Semaphore(per_host_cap)
                host_semaphores[host] = sem
            return sem

    def _wrapped(work: Callable[[], _StreamResult], host: str) -> _StreamResult:
        sem = _sem_for(host)
        with sem:
            try:
                return work()
            except Exception as e:
                # Belt-and-braces: workers already trap their own
                # exceptions and return errors=[...], but if one leaks
                # we still isolate rather than kill the whole executor.
                return _StreamResult(errors=[f"stream_worker_crashed: {e}"])

    results: list[_StreamResult] = [None] * len(work_items)  # type: ignore[list-item]
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_idx = {}
        for i, (work, host) in enumerate(work_items):
            future_to_idx[pool.submit(_wrapped, work, host)] = i
        for fut in as_completed(future_to_idx):
            results[future_to_idx[fut]] = fut.result()
    return results


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

    # Build a flat work list of (worker_callable, host_key) tuples. Every
    # per-source and per-stream skip decision happens here; workers see
    # only the concrete work-to-do. Serial and concurrent paths dispatch
    # the SAME callables so behavior is identical either way.
    work_items: list[tuple[Callable[[], _StreamResult], str]] = []
    stream_labels: list[str] = []  # parallel array for log messages

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
            if bool(stream.get("paused")):
                log.info(
                    "stream_paused",
                    source=src.get("id"), type=source_type, stream=stream_name,
                    reason="paused for this stream on the /sources page",
                )
                continue

            work = _make_stream_worker(
                source=source, source_type=source_type,
                stream=stream, stream_name=stream_name,
                fetching=fetching, effective_since=effective_since,
                effective_until=effective_until, advance_cursor=advance_cursor,
                raw_root=raw_root, week_id=week_id,
            )
            work_items.append((work, _host_key(source_type, stream)))
            stream_labels.append(stream_name)

    # Dispatch: concurrent when the flag is on, serial otherwise. Serial
    # path preserves the pre-ADR-0023 inter-stream sleep for host politeness.
    from pipeline import features as _feat
    concurrency_on = _feat.enabled("fetch_concurrency_enabled", product_id or None)

    if concurrency_on and work_items:
        max_workers = int(fetching.get("max_concurrent_streams", 4))
        per_host_cap = int(fetching.get("max_concurrent_per_host", 1))
        log.info(
            "fetch_concurrent", streams=len(work_items),
            max_workers=max_workers, per_host_cap=per_host_cap,
        )
        stream_results = _dispatch(
            work_items, max_workers=max_workers, per_host_cap=per_host_cap,
        )
    else:
        stream_results = []
        sleep_between = float(fetching.get("sleep_between_streams_seconds", 0))
        for i, (work, _host) in enumerate(work_items):
            stream_results.append(work())
            if sleep_between > 0 and i < len(work_items) - 1:
                time.sleep(sleep_between)

    # Reduce per-stream results into the run-level counters / completeness /
    # errors. Single-threaded reducer — safe regardless of dispatch mode.
    for label, res in zip(stream_labels, stream_results):
        counters["fetched"] += res.fetched
        counters["deduped"] += res.deduped
        counters["out_of_window"] += res.out_of_window
        completeness["ceiling_hits"].extend(res.ceiling_hits)
        completeness["comment_cap_hits"].extend(res.comment_cap_hits)
        for e in res.errors:
            errors.append(f"stream {label} failed: {e}")

    return {"counters": counters, "completeness": completeness, "errors": errors}


def _make_stream_worker(
    *,
    source, source_type: str, stream: dict[str, Any], stream_name: str,
    fetching: dict[str, Any], effective_since: float | None,
    effective_until: float | None, advance_cursor: bool,
    raw_root: Path, week_id: str,
) -> Callable[[], _StreamResult]:
    """Bind one stream's fetch loop into a zero-arg callable. The body
    is the SAME logic that used to live inline in `run_fetch`; extracting
    it lets us dispatch identically through the concurrent executor or
    a plain for-loop."""

    cfg = {**fetching, **stream}

    def _work() -> _StreamResult:
        result = _StreamResult()
        # Effective floor: explicit override wins over the persisted cursor.
        if effective_since is not None:
            floor_ts = effective_since
        else:
            floor_ts = storage.get_cursor(source_type, stream_name)
        cursor = SourceCursor(cursor_ts=floor_ts)
        stats = FetchStats()

        try:
            # Collect the whole stream first, then dedupe in ONE
            # filter_unseen call (batching from perf fix #6).
            raw_items: list[Any] = []
            for raw_item in source.fetch_since(cursor, cfg, stats):
                if effective_until is not None:
                    ts = raw_item.created_at.timestamp()
                    if ts > effective_until:
                        result.out_of_window += 1
                        continue
                raw_items.append(raw_item)

            unseen = storage.filter_unseen(
                source_type, [ri.external_id for ri in raw_items]
            )
            batch: list[dict[str, Any]] = []
            fresh_ids: list[str] = []
            for raw_item in raw_items:
                if raw_item.external_id not in unseen:
                    result.deduped += 1
                    continue
                fresh_ids.append(raw_item.external_id)
                batch.append(_serialize(raw_item, week_id))

            if batch:
                path = _raw_path(raw_root, source_type, week_id, stream_name)
                append_jsonl(path, batch)
                storage.mark_seen(source_type, fresh_ids)
                result.fetched += len(batch)

            if advance_cursor and cursor.cursor_ts is not None:
                storage.set_cursor(source_type, stream_name, cursor.cursor_ts)

            for ch in stats.ceiling_hits:
                result.ceiling_hits.append(list(ch))
            for cc in stats.comment_cap_hits:
                result.comment_cap_hits.append(list(cc))

            log.info(
                "stream_fetched", stream=stream_name, fetched=len(batch),
                ceiling_hits=len(stats.ceiling_hits),
            )
        except Exception as e:  # per-stream isolation (§4.3 step 5)
            log.error("stream_failed", stream=stream_name, error=str(e))
            result.errors.append(str(e))
        return result

    return _work


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
