"""Wizard v2 mini-fetch runner (Phase 4).

Runs a tiny, throw-away fetch across the wizard draft's *enabled + keyless*
suggested sources. Purpose is to gather a small corpus of real posts so the
user can calibrate the classifier on real data (Screen 3) and the taxonomy
proposer (Phase 5) can generate areas grounded in actual content.

Non-goals: *do not* touch the product warehouse, cursors, or seen-ids
tables. All state lives under `data/.wizard/<draft_slug>/`:

    status.json      — {status, started_at, per_source[], corpus_size, error}
    minifetch.jsonl  — one normalized post per line

Runs in a background thread launched by the wizard route. HTMX (or a plain
poll) hits `read_status()` to render progress.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import structlog

log = structlog.get_logger()


# --- Config caps -------------------------------------------------------------
MAX_TOTAL_ITEMS = 50            # hard ceiling across all sources
MAX_PER_SOURCE = 20             # per-source cap so one source can't dominate
MAX_WALL_CLOCK_SECONDS = 20.0   # abort loop when we hit this
TIGHT_MAX_PAGES_PER_QUERY = 1   # override for suggested-source stream configs
TIGHT_HITS_PER_PAGE = 30

# Statuses used in status.json.
STATUS_NOT_STARTED = "not_started"
STATUS_FETCHING = "fetching"
STATUS_READY = "ready"
STATUS_EMPTY = "empty"          # ran to completion, < 3 items collected
STATUS_ERROR = "error"          # unrecoverable error before any items

MIN_USEFUL_ITEMS = 3            # zero-results threshold


@dataclass
class SourceProgress:
    """One line in status.per_source[]."""

    plugin_id: str
    items: int = 0
    status: str = "pending"     # pending | ok | error
    error: str = ""


@dataclass
class MinifetchStatus:
    status: str = STATUS_NOT_STARTED
    started_at: str = ""
    finished_at: str = ""
    corpus_size: int = 0
    per_source: list[SourceProgress] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "corpus_size": self.corpus_size,
            "per_source": [asdict(p) for p in self.per_source],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MinifetchStatus":
        return cls(
            status=d.get("status", STATUS_NOT_STARTED),
            started_at=d.get("started_at", ""),
            finished_at=d.get("finished_at", ""),
            corpus_size=int(d.get("corpus_size", 0)),
            per_source=[SourceProgress(**p) for p in (d.get("per_source") or [])],
            error=d.get("error", ""),
        )


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def wizard_data_root() -> Path:
    """`data/.wizard/` — kept out of the per-product warehouse namespace on
    purpose so pipeline code never sees these files."""
    from pipeline.config import app_config, resolve_path
    base = resolve_path(app_config()["paths"]["data_root"])
    return base / ".wizard"


def _draft_dir(slug: str) -> Path:
    if not slug or "/" in slug or "\\" in slug or ".." in slug:
        raise ValueError(f"invalid draft slug: {slug!r}")
    return wizard_data_root() / slug


def status_path(slug: str) -> Path:
    return _draft_dir(slug) / "status.json"


def corpus_path(slug: str) -> Path:
    return _draft_dir(slug) / "minifetch.jsonl"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_status(slug: str) -> MinifetchStatus:
    """Return the current status. Absent file → NOT_STARTED."""
    try:
        p = status_path(slug)
    except ValueError:
        return MinifetchStatus(status=STATUS_ERROR, error="invalid slug")
    if not p.exists():
        return MinifetchStatus()
    try:
        return MinifetchStatus.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception:
        return MinifetchStatus()


def _write_status(slug: str, status: MinifetchStatus) -> None:
    d = _draft_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    tmp = status_path(slug).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(status.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(status_path(slug))


def load_corpus(slug: str) -> list[dict[str, Any]]:
    """Return the minifetch corpus as normalized dicts (one per line)."""
    try:
        p = corpus_path(slug)
    except ValueError:
        return []
    if not p.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except Exception:
            continue
    return items


def discard_corpus(slug: str) -> None:
    """Remove the wizard temp dir for a slug. Called on product materialization
    or draft discard."""
    try:
        d = _draft_dir(slug)
    except ValueError:
        return
    if not d.exists():
        return
    for p in d.iterdir():
        try:
            p.unlink()
        except Exception:
            pass
    try:
        d.rmdir()
    except Exception:
        pass


def start_minifetch(slug: str, suggested_sources: list[dict[str, Any]]) -> None:
    """Launch the runner in a daemon background thread.

    `suggested_sources` are the items from `WizardV2Draft.suggested_sources`.
    Only entries with `enabled=True` AND `requires_key=False` are actually
    fetched — everything else is recorded as "pending" in per_source but
    skipped in-run. Idempotent: calling twice on a running slug is a no-op.
    """
    st = read_status(slug)
    if st.status == STATUS_FETCHING:
        log.info("minifetch.already_running", slug=slug)
        return
    thread = threading.Thread(
        target=_run_in_thread, args=(slug, suggested_sources),
        daemon=True, name=f"minifetch-{slug}",
    )
    thread.start()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _run_in_thread(slug: str, suggested_sources: list[dict[str, Any]]) -> None:
    """Thread body — writes status incrementally so the polling UI sees
    progress. Never raises to the thread boundary."""
    try:
        _run_minifetch(slug, suggested_sources)
    except Exception as e:
        log.exception("minifetch.thread_crashed", slug=slug, error=str(e))
        st = read_status(slug)
        st.status = STATUS_ERROR
        st.error = str(e)
        st.finished_at = _now_iso()
        _write_status(slug, st)


def _run_minifetch(slug: str, suggested_sources: list[dict[str, Any]]) -> None:
    from sources import get_source
    from sources.base import FetchStats, SourceCursor

    started = time.monotonic()
    d = _draft_dir(slug)
    d.mkdir(parents=True, exist_ok=True)

    # Reset the corpus + status.
    corpus_p = corpus_path(slug)
    if corpus_p.exists():
        corpus_p.unlink()
    corpus_p.write_text("", encoding="utf-8")

    enabled_srcs = [
        s for s in suggested_sources
        if s.get("enabled") and not s.get("requires_key")
    ]
    status = MinifetchStatus(
        status=STATUS_FETCHING,
        started_at=_now_iso(),
        per_source=[SourceProgress(plugin_id=s.get("plugin_id", "?"))
                    for s in enabled_srcs],
    )
    _write_status(slug, status)

    total_items = 0

    for progress, src_cfg in zip(status.per_source, enabled_srcs):
        plugin_id = src_cfg.get("plugin_id", "")
        stream_cfg_in = dict(src_cfg.get("stream_config") or {})

        # If the Step 3 form collected multiple per-stream identifiers
        # (e.g. two subreddits), expand into a list of stream configs
        # and fetch each one. Extras came from `_extra_streams` — see
        # webui.wizard._apply_inline_stream_config.
        extras = stream_cfg_in.pop("_extra_streams", None) or []
        stream_variants: list[dict[str, Any]] = []
        base = {
            **stream_cfg_in,
            "name": stream_cfg_in.get("name") or f"wizard-{slug}-{plugin_id}",
            "max_pages_per_query": TIGHT_MAX_PAGES_PER_QUERY,
            "hits_per_page": TIGHT_HITS_PER_PAGE,
        }
        stream_variants.append(base)
        for i, extra in enumerate(extras):
            if not isinstance(extra, dict):
                continue
            variant = {
                **base,      # inherit tightened caps
                **extra,     # override per-stream identifiers
                "name": extra.get("name") or f"wizard-{slug}-{plugin_id}-{i + 2}",
            }
            stream_variants.append(variant)

        # Wall-clock cap.
        if time.monotonic() - started > MAX_WALL_CLOCK_SECONDS:
            progress.status = "error"
            progress.error = "wall-clock cap reached before source started"
            _write_status(slug, status)
            continue

        try:
            source = get_source(plugin_id)
        except Exception as e:
            progress.status = "error"
            progress.error = f"init failed: {e}"
            _write_status(slug, status)
            continue

        try:
            per_source_items = 0
            source_items: list[dict[str, Any]] = []
            variant_failures: list[str] = []
            for stream_cfg in stream_variants:
                cursor = SourceCursor(cursor_ts=None)
                stats = FetchStats()
                try:
                    for raw_item in source.fetch_since(cursor, stream_cfg, stats):
                        source_items.append(_serialize(raw_item))
                        per_source_items += 1
                        if per_source_items >= MAX_PER_SOURCE:
                            break
                        if total_items + per_source_items >= MAX_TOTAL_ITEMS:
                            break
                        if time.monotonic() - started > MAX_WALL_CLOCK_SECONDS:
                            break
                except Exception as e:
                    variant_failures.append(
                        f"stream {stream_cfg.get('name')}: {str(e)[:120]}"
                    )
                if (per_source_items >= MAX_PER_SOURCE
                        or total_items + per_source_items >= MAX_TOTAL_ITEMS
                        or time.monotonic() - started > MAX_WALL_CLOCK_SECONDS):
                    break

            _append_corpus(corpus_p, source_items)
            total_items += per_source_items
            progress.items = per_source_items
            if source_items or not variant_failures:
                progress.status = "ok"
                if variant_failures:
                    # Some streams failed but at least one worked; surface a
                    # short note without dropping the whole source.
                    progress.error = "; ".join(variant_failures)[:200]
            else:
                progress.status = "error"
                progress.error = "; ".join(variant_failures)[:200]
        except Exception as e:
            progress.status = "error"
            progress.error = str(e)[:200]
        status.corpus_size = total_items
        _write_status(slug, status)
        if total_items >= MAX_TOTAL_ITEMS:
            break
        if time.monotonic() - started > MAX_WALL_CLOCK_SECONDS:
            break

    status.finished_at = _now_iso()
    if total_items >= MIN_USEFUL_ITEMS:
        status.status = STATUS_READY
    elif not any(p.status == "error" for p in status.per_source):
        status.status = STATUS_EMPTY
    else:
        # At least one source errored AND we have few items — call it empty.
        # The UI treats EMPTY the same as READY-but-too-few (zero-results path).
        status.status = STATUS_EMPTY
    _write_status(slug, status)


def _serialize(raw_item) -> dict[str, Any]:
    """Normalize a RawItem into the wizard corpus shape (subset of the
    warehouse item shape — no week_id, source_id derived from
    source_display_name)."""
    return {
        "id": f"{raw_item.source}:{raw_item.external_id}",
        "external_id": raw_item.external_id,
        "source": raw_item.source,
        "source_display_name": raw_item.source_display_name,
        "url": raw_item.url,
        "title": raw_item.title or "",
        "body": raw_item.body or "",
        "author": raw_item.author or "",
        "engagement": raw_item.engagement,
        "created_at": raw_item.created_at.isoformat(),
    }


def _append_corpus(path: Path, items: Iterable[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for it in items:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Deck sampling for the calibration UI
# ---------------------------------------------------------------------------


def sample_deck(slug: str, size: int = 10,
                exclude_ids: Optional[Iterable[str]] = None) -> list[dict[str, Any]]:
    """Return up to `size` items from the corpus, round-robin across sources
    for diversity, excluding ids the user has already judged."""
    corpus = load_corpus(slug)
    if not corpus:
        return []
    excluded = set(exclude_ids or [])
    by_source: dict[str, list[dict[str, Any]]] = {}
    for it in corpus:
        if it.get("id") in excluded:
            continue
        by_source.setdefault(it.get("source_display_name") or it.get("source") or "?", []).append(it)
    out: list[dict[str, Any]] = []
    while len(out) < size and any(by_source.values()):
        for k in list(by_source.keys()):
            bucket = by_source[k]
            if not bucket:
                continue
            out.append(bucket.pop(0))
            if len(out) >= size:
                break
    return out
