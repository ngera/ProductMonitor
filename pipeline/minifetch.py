"""Wizard v2 mini-fetch runner (Phase 4).

Runs a tiny, throw-away fetch across the wizard draft's *enabled + keyless*
suggested sources. Purpose is to gather a small corpus of real posts so the
user can calibrate the classifier on real data (Screen 3) and the taxonomy
proposer (Phase 5) can generate areas grounded in actual content.

Fetch quality (Slice A/B/C/D/E — 2026-07-29):

  - Each yielded item is scored against the draft's `display + aliases` +
    WATCHLIST_RE (KB/CVE hits). Non-matching items are kept only up to a
    per-source quota (`UNMATCHED_QUOTA_PER_SOURCE`) so calibration keeps
    both polarities without drowning in noise. Every drop is counted in
    `SourceProgress.filtered_out` so the calibrate page can nudge the
    user to trim noisy feeds.
  - HN streams get widened with aliases at fetch time (aliases are on the
    draft but not yet in stream_config).
  - `sample_deck` sorts by score descending and reserves ~30% low-score
    slots so negatives survive.
  - Optional LLM relevance gate (assistant LLM, ad-hoc facts) when the
    caller passes `use_llm_gate=True` on `start_minifetch`.

Non-goals: *do not* touch the product warehouse, cursors, or seen-ids
tables. All state lives under `data/.wizard/<draft_slug>/`:

    status.json      — {status, started_at, per_source[], corpus_size, error}
    minifetch.jsonl  — one normalized post per line

Runs in a background thread launched by the wizard route. HTMX (or a plain
poll) hits `read_status()` to render progress.
"""

from __future__ import annotations

import json
import re
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

# Per-source quota for items that DIDN'T match the product's keywords.
# A few negatives are useful for calibration (they let the user teach the
# gate what "not relevant" looks like); more than a few is noise.
UNMATCHED_QUOTA_PER_SOURCE = 3

# Reuse the pipeline filter's watchlist regex (KB####/CVE) so an item
# matching either survives even when it doesn't hit the product's terms.
from pipeline.filter import WATCHLIST_RE  # noqa: E402

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
    items: int = 0              # items kept after all gates (shown on deck)
    fetched_raw: int = 0        # items yielded by fetch before any gating
    filtered_out: int = 0       # keyword-gate rejects
    llm_dropped: int = 0        # LLM-gate rejects (0 when not enabled)
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


def start_minifetch(
    slug: str,
    suggested_sources: list[dict[str, Any]],
    *,
    product_facts: Optional[dict[str, Any]] = None,
    use_llm_gate: bool = False,
) -> None:
    """Launch the runner in a daemon background thread.

    `suggested_sources` are the items from `WizardV2Draft.suggested_sources`.
    Only entries with `enabled=True` AND `requires_key=False` are actually
    fetched — everything else is recorded as "pending" in per_source but
    skipped in-run. Idempotent: calling twice on a running slug is a no-op.

    `product_facts`, when provided, is used for the keyword pre-gate and
    (if `use_llm_gate`) the LLM relevance pass. Recognized keys:
      display: str, aliases: list[str], scope_in: list[str],
      scope_out: list[str], description: str.

    `use_llm_gate` runs an assistant-LLM relevance call on every item that
    survives the cheap keyword pass. Adds ~$0.01 + 5-15s per calibration
    but produces a much cleaner deck. No-op when the assistant LLM isn't
    configured — logs a warning and falls back to keyword-only gating.
    """
    st = read_status(slug)
    if st.status == STATUS_FETCHING:
        log.info("minifetch.already_running", slug=slug)
        return
    thread = threading.Thread(
        target=_run_in_thread,
        args=(slug, suggested_sources, product_facts or {}, bool(use_llm_gate)),
        daemon=True, name=f"minifetch-{slug}",
    )
    thread.start()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _run_in_thread(
    slug: str,
    suggested_sources: list[dict[str, Any]],
    product_facts: Optional[dict[str, Any]] = None,
    use_llm_gate: bool = False,
) -> None:
    """Thread body — writes status incrementally so the polling UI sees
    progress. Never raises to the thread boundary."""
    try:
        _run_minifetch(slug, suggested_sources, product_facts or {}, use_llm_gate)
    except Exception as e:
        log.exception("minifetch.thread_crashed", slug=slug, error=str(e))
        st = read_status(slug)
        st.status = STATUS_ERROR
        st.error = str(e)
        st.finished_at = _now_iso()
        _write_status(slug, st)


def _keyword_terms(product_facts: dict[str, Any]) -> list[str]:
    """Lowercased search terms — product display + every declared alias.

    Empty entries dropped. Terms shorter than 3 chars are also dropped so a
    stray one-letter alias doesn't match every post. Order preserved (display
    first) so the score gives slightly more weight to the primary name.
    """
    display = (product_facts.get("display") or "").strip()
    aliases = [str(a).strip() for a in (product_facts.get("aliases") or [])]
    seen: set[str] = set()
    out: list[str] = []
    for t in [display, *aliases]:
        t = t.lower().strip()
        if len(t) < 3 or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _score_item(item: dict[str, Any], terms: list[str]) -> tuple[int, int, int]:
    """Return (match_count, watchlist_hits, engagement_norm).

    - match_count: how many terms appear in title+body (case-insensitive
      substring). Not word-boundary-strict because product names can appear
      inside URLs, code blocks, hyphenated words, etc.
    - watchlist_hits: KB/CVE/build regex hits — proxy signal that an item
      is grounded in something concrete even without a name match.
    - engagement_norm: 0-10 scalar derived from the max numeric field on
      the engagement blob. Present on HN/Reddit; absent on RSS/media.
    """
    hay = (
        (item.get("title") or "")
        + "\n"
        + (item.get("body") or "")
    ).lower()
    matches = sum(1 for t in terms if t and t in hay)
    watchlist = 1 if WATCHLIST_RE.search(hay) else 0
    eng = item.get("engagement") or {}
    if isinstance(eng, dict):
        try:
            vals = [
                float(v) for v in eng.values()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            ]
            eng_scalar = max(vals) if vals else 0.0
        except Exception:
            eng_scalar = 0.0
    else:
        eng_scalar = 0.0
    # Log-normalize so a viral post (thousands of upvotes) doesn't dominate.
    import math
    eng_norm = min(10, int(math.log1p(max(eng_scalar, 0)) * 2))
    return matches, watchlist, eng_norm


def _relevance_score(matches: int, watchlist: int, eng_norm: int) -> int:
    """Composite relevance score used for deck ordering.

    Weights: name match dominates, watchlist is a strong tiebreaker,
    engagement is a mild bonus so on-topic viral items rank above
    on-topic obscure ones.
    """
    return matches * 10 + watchlist * 5 + eng_norm


def _widen_hn_streams_with_aliases(
    stream_variants: list[dict[str, Any]],
    plugin_id: str,
    product_facts: dict[str, Any],
    slug: str,
) -> list[dict[str, Any]]:
    """Slice C — extend HN stream variants with per-alias search_queries.

    HN's Algolia backend keys on search_queries. Every alias that isn't
    already in the query list gets appended so we cast a wider net on the
    calibration deck without waiting for the user to hand-edit sources.yaml.
    """
    if plugin_id != "hn":
        return stream_variants
    aliases = [str(a).strip() for a in (product_facts.get("aliases") or []) if a]
    if not aliases:
        return stream_variants
    out: list[dict[str, Any]] = []
    for variant in stream_variants:
        cur_qs = list(variant.get("search_queries") or [])
        cur_lower = {q.lower() for q in cur_qs}
        new_qs = list(cur_qs) + [a for a in aliases if a.lower() not in cur_lower]
        if new_qs != cur_qs:
            merged = dict(variant)
            merged["search_queries"] = new_qs
            out.append(merged)
        else:
            out.append(variant)
    return out


def _llm_gate_survivors(
    survivors: list[dict[str, Any]],
    product_facts: dict[str, Any],
    started: float,
    budget_seconds: float = 15.0,
) -> tuple[list[dict[str, Any]], int]:
    """Slice E — run the assistant LLM's relevance gate on `survivors`.

    Returns (kept, dropped_count). Fail-open: if the assistant LLM isn't
    configured, or a call raises, that particular item is kept. Also
    bounded by `budget_seconds` — once we've spent that much on this pass,
    we stop calling and keep the rest.

    Uses the shared ad-hoc helper in pipeline.relevance so per-product
    ProductSpec doesn't need to be loaded.
    """
    try:
        from pipeline.relevance import evaluate_ad_hoc
    except Exception:
        return survivors, 0
    kept: list[dict[str, Any]] = []
    dropped = 0
    deadline = time.monotonic() + budget_seconds
    for it in survivors:
        if time.monotonic() > deadline:
            kept.append(it)
            continue
        try:
            res = evaluate_ad_hoc(
                title=it.get("title") or "",
                body=it.get("body") or "",
                product_facts=product_facts,
            )
        except Exception as e:
            log.warning("minifetch.llm_gate_failed", error=str(e)[:120])
            kept.append(it)
            continue
        if res is None:
            # Assistant LLM not configured — keep everything, no drops.
            return survivors, 0
        if (not res.relevant) and res.confidence >= 0.7:
            dropped += 1
            continue
        kept.append(it)
    return kept, dropped


def _run_minifetch(
    slug: str,
    suggested_sources: list[dict[str, Any]],
    product_facts: Optional[dict[str, Any]] = None,
    use_llm_gate: bool = False,
) -> None:
    product_facts = product_facts or {}
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

    terms = _keyword_terms(product_facts)
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

        # Slice C — HN benefits from alias-widened queries at calibration time.
        stream_variants = _widen_hn_streams_with_aliases(
            stream_variants, plugin_id, product_facts, slug,
        )

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
            source_items: list[dict[str, Any]] = []
            variant_failures: list[str] = []
            raw_seen = 0
            filtered_out = 0
            unmatched_kept = 0     # non-matching items retained (quota)
            for stream_cfg in stream_variants:
                cursor = SourceCursor(cursor_ts=None)
                stats = FetchStats()
                try:
                    for raw_item in source.fetch_since(cursor, stream_cfg, stats):
                        raw_seen += 1
                        serialized = _serialize(raw_item)
                        # Slice A — cheap keyword gate. Score, decide.
                        matches, watchlist, eng_norm = _score_item(serialized, terms)
                        keep = False
                        if not terms:
                            # No product facts supplied — behave like old
                            # minifetch and keep everything (avoid regressions
                            # for callers that haven't been updated yet).
                            keep = True
                        elif matches > 0 or watchlist > 0:
                            keep = True
                        elif unmatched_kept < UNMATCHED_QUOTA_PER_SOURCE:
                            keep = True
                            unmatched_kept += 1
                        if not keep:
                            filtered_out += 1
                        else:
                            serialized["_score"] = _relevance_score(
                                matches, watchlist, eng_norm,
                            )
                            serialized["_matches"] = matches
                            source_items.append(serialized)
                        if len(source_items) >= MAX_PER_SOURCE:
                            break
                        if total_items + len(source_items) >= MAX_TOTAL_ITEMS:
                            break
                        if time.monotonic() - started > MAX_WALL_CLOCK_SECONDS:
                            break
                except Exception as e:
                    variant_failures.append(
                        f"stream {stream_cfg.get('name')}: {str(e)[:120]}"
                    )
                if (len(source_items) >= MAX_PER_SOURCE
                        or total_items + len(source_items) >= MAX_TOTAL_ITEMS
                        or time.monotonic() - started > MAX_WALL_CLOCK_SECONDS):
                    break

            # Slice E — optional LLM relevance pass on survivors.
            llm_dropped = 0
            if use_llm_gate and source_items:
                source_items, llm_dropped = _llm_gate_survivors(
                    source_items, product_facts, started,
                )

            _append_corpus(corpus_p, source_items)
            per_source_items = len(source_items)
            total_items += per_source_items
            progress.items = per_source_items
            progress.fetched_raw = raw_seen
            progress.filtered_out = filtered_out
            progress.llm_dropped = llm_dropped
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
    """Return up to `size` items, prioritizing product-relevant ones while
    reserving ~30% of the deck for negatives so calibration keeps both
    polarities.

    Selection strategy (Slice B):
      1. Sort within each source bucket by descending `_score` (populated
         at fetch time by `_score_item`). Items without a score sort last.
      2. Reserve `NEGATIVE_SLOTS = ceil(size * 0.3)` slots at the end for
         the lowest-scored items across all sources — these are the
         "unmatched quota" survivors that let the user teach the gate what
         off-topic looks like.
      3. Fill the remaining slots by round-robin across source buckets in
         score-descending order, so no single source dominates even when
         it has all the highest-scoring items.

    Old callers get the same list-of-dicts shape, just ordered better.
    """
    import math
    corpus = load_corpus(slug)
    if not corpus:
        return []
    excluded = set(exclude_ids or [])
    remaining = [it for it in corpus if it.get("id") not in excluded]
    if not remaining:
        return []

    # Score-descending order globally, then split reserve.
    def _score(it: dict[str, Any]) -> int:
        try:
            return int(it.get("_score") or 0)
        except (TypeError, ValueError):
            return 0
    remaining.sort(key=_score, reverse=True)

    negative_slots = max(0, min(math.ceil(size * 0.3), len(remaining) - 1))
    positive_slots = size - negative_slots

    # High-score pool: everything above the reserve slice. Round-robin
    # within so diverse sources are represented up top.
    top_pool = remaining[: max(0, len(remaining) - negative_slots)]
    by_source: dict[str, list[dict[str, Any]]] = {}
    for it in top_pool:
        key = it.get("source_display_name") or it.get("source") or "?"
        by_source.setdefault(key, []).append(it)

    out: list[dict[str, Any]] = []
    while len(out) < positive_slots and any(by_source.values()):
        for k in list(by_source.keys()):
            bucket = by_source[k]
            if not bucket:
                continue
            out.append(bucket.pop(0))
            if len(out) >= positive_slots:
                break

    # Fill remaining slots from the low-score tail (already sorted).
    if len(out) < size:
        # Prefer items not already in `out` (dedup by id).
        seen_ids = {it.get("id") for it in out}
        tail = remaining[len(remaining) - negative_slots :] if negative_slots else []
        for it in tail:
            if len(out) >= size:
                break
            if it.get("id") in seen_ids:
                continue
            out.append(it)
            seen_ids.add(it.get("id"))
        # If we still have slack (small corpus), backfill from anything left.
        if len(out) < size:
            for it in remaining:
                if len(out) >= size:
                    break
                if it.get("id") in seen_ids:
                    continue
                out.append(it)
                seen_ids.add(it.get("id"))
    return out
