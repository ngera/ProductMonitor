"""Persistent-issue stage — cluster this run's week_groups against prior weeks.

See [ADR 0016](../documents/decisions/0016-persistent-issue-stage.md) and
[report_v2_design.md §5.1](../documents/report_v2_design.md).

Section-scoped identity: a "bug that's also a feature request" gets one
persistent-issue-id in the Bugs section and a separate one in the Features
section. Forward-only — historical week_groups without persistent-issue-ids
stay that way; they show only current-week counts in the digest.

Runs between `group` and `aggregate` when `digest_v2_enabled` is on. Loads
sentence-transformers lazily so the process cost is only paid when the
feature actually runs.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Optional

import structlog

from pipeline import storage
from pipeline.config import app_config, current_product

log = structlog.get_logger()


SECTIONS = ("bugs", "features", "positive", "negative")


# Cached embedder — loaded on first call, kept for process lifetime.
_EMBEDDER = None


def _digest_cfg() -> dict:
    return app_config().get("digest", {}) or {}


def _threshold() -> float:
    return float(_digest_cfg().get("persistent_issue_threshold", 0.85))


def _positive_min() -> float:
    return float((_digest_cfg().get("sentiment_thresholds") or {}).get("positive", 0.2))


def _negative_max() -> float:
    return float((_digest_cfg().get("sentiment_thresholds") or {}).get("negative", -0.2))


def _get_embedder():
    """Load the sentence-transformers model once per process."""
    global _EMBEDDER
    if _EMBEDDER is None:
        # Lazy import: only pay the torch import cost when the stage runs.
        from sentence_transformers import SentenceTransformer  # type: ignore
        model_name = _digest_cfg().get("embedding_model", "all-MiniLM-L6-v2")
        log.info("persistent_issue_loading_embedder", model=model_name)
        _EMBEDDER = SentenceTransformer(model_name)
    return _EMBEDDER


def matches_section(row: dict, section: str) -> bool:
    """Does this canonical-item classification place its group in `section`?

    Pure function (no I/O). `row` is a dict with the keys produced by
    `_week_groups_query` below — `content_types_json` and `sentiment`.
    """
    content_types = set(json.loads(row.get("content_types_json") or "[]"))
    sentiment = row.get("sentiment")
    if section == "bugs":
        return "bug_report" in content_types
    if section == "features":
        return "feature_request" in content_types
    if section == "positive":
        return sentiment is not None and sentiment > _positive_min()
    if section == "negative":
        return sentiment is not None and sentiment < _negative_max()
    return False


def canonical_text(row: dict) -> str:
    """Text sent to the embedder: title + summary, trimmed for tokenization."""
    title = (row.get("title") or "").strip()
    summary = (row.get("summary") or "").strip()
    return f"{title}. {summary}".strip(". ").strip()[:1000]


def cosine_similarity(a, b) -> float:
    """Pure cosine similarity on two 1-D numeric sequences.

    Numpy-free by design so unit tests don't need to install torch/numpy.
    Works uniformly on Python lists and numpy arrays (numpy arrays iterate
    as scalars). For 384-dim embeddings called O(N) times per section this
    is fast enough — the hot path is the embedder call itself, not this.
    """
    import math
    dot = 0.0
    norm_a_sq = 0.0
    norm_b_sq = 0.0
    for x, y in zip(a, b):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        norm_a_sq += fx * fx
        norm_b_sq += fy * fy
    denom = math.sqrt(norm_a_sq) * math.sqrt(norm_b_sq)
    return dot / denom if denom else 0.0


def _load_prior_issues(product_id: str, section: str) -> list[dict]:
    """Load all prior persistent issues for this product+section, embeddings included."""
    import numpy as np
    rows = storage.query(
        "SELECT issue_id, canonical_title, first_seen_week, last_seen_week, "
        "total_mentions, embedding_blob FROM persistent_issues "
        "WHERE product_id=? AND section=?",
        [product_id, section],
    )
    out: list[dict] = []
    for r in rows:
        blob = r.get("embedding_blob")
        if blob is None:
            continue
        emb = np.frombuffer(blob, dtype=np.float32)
        out.append({
            "issue_id": r["issue_id"],
            "canonical_title": r["canonical_title"],
            "first_seen_week": r["first_seen_week"],
            "last_seen_week": r["last_seen_week"],
            "total_mentions": int(r["total_mentions"] or 0),
            "embedding": emb,
        })
    return out


def _week_groups_query(week_id: str) -> list[dict]:
    """Load this week's groups joined with their canonical items' classifications.

    Section membership is decided from `content_types_json` + `sentiment`.
    Groups whose canonical item lacks a classification (rare — group runs
    after classify) are quietly excluded.
    """
    return storage.query(
        "SELECT wg.week_id, wg.area, wg.group_key, wg.canonical_item_id, wg.member_count, "
        "i.title, ic.content_types_json, ic.sentiment, ic.summary "
        "FROM week_groups wg "
        "JOIN items i ON i.id = wg.canonical_item_id "
        "LEFT JOIN item_classifications ic ON ic.item_id = wg.canonical_item_id "
        "WHERE wg.week_id=?",
        [week_id],
    )


def run(week_id: str) -> dict[str, Any]:
    """Compute persistent-issue-ids for `week_id`'s groups across all sections.

    Returns a counters dict for the run log. Idempotent: rerunning the stage
    updates existing mappings in place (embeddings for the same canonical
    text are deterministic under the pinned model).
    """
    try:
        product_id = current_product().id
    except Exception:
        log.warning("persistent_issue_no_product")
        return {"counters": {}, "skipped": "no_product"}

    all_wgs = _week_groups_query(week_id)
    if not all_wgs:
        log.info("persistent_issue_no_groups", week_id=week_id)
        return {"counters": {"groups": 0}}

    embedder = _get_embedder()
    threshold = _threshold()

    counters = {"sections": 0, "groups": 0, "matched": 0, "new": 0}

    for section in SECTIONS:
        section_wgs = [wg for wg in all_wgs if matches_section(wg, section)]
        if not section_wgs:
            continue
        counters["sections"] += 1

        prior = _load_prior_issues(product_id, section)
        # Encode this section's canonicals in one batch call for speed.
        texts = [canonical_text(wg) for wg in section_wgs]
        embs = embedder.encode(texts, convert_to_numpy=True, show_progress_bar=False)

        for wg, emb in zip(section_wgs, embs):
            counters["groups"] += 1
            import numpy as np
            emb_f32 = emb.astype(np.float32)

            best: Optional[dict] = None
            best_sim = -1.0
            for p in prior:
                sim = cosine_similarity(emb_f32, p["embedding"])
                if sim > best_sim:
                    best_sim = sim
                    best = p

            if best is not None and best_sim >= threshold:
                issue_id = best["issue_id"]
                counters["matched"] += 1
                storage.execute(
                    "UPDATE persistent_issues SET last_seen_week=?, "
                    "total_mentions=total_mentions+? "
                    "WHERE product_id=? AND section=? AND issue_id=?",
                    [
                        week_id, int(wg.get("member_count") or 0),
                        product_id, section, issue_id,
                    ],
                )
            else:
                issue_id = str(uuid.uuid4())
                counters["new"] += 1
                storage.execute(
                    "INSERT INTO persistent_issues(product_id, section, issue_id, "
                    "canonical_title, first_seen_week, last_seen_week, "
                    "total_mentions, embedding_blob) VALUES (?,?,?,?,?,?,?,?)",
                    [
                        product_id, section, issue_id,
                        wg.get("title") or "",
                        week_id, week_id,
                        int(wg.get("member_count") or 0),
                        emb_f32.tobytes(),
                    ],
                )
                # Append to prior so subsequent groups in this section can
                # match against the just-created issue (same-week dedup).
                prior.append({
                    "issue_id": issue_id,
                    "canonical_title": wg.get("title") or "",
                    "first_seen_week": week_id,
                    "last_seen_week": week_id,
                    "total_mentions": int(wg.get("member_count") or 0),
                    "embedding": emb_f32,
                })

            storage.execute(
                "INSERT INTO week_group_persistent_issue(week_id, area, group_key, "
                "section, issue_id) VALUES (?,?,?,?,?) "
                "ON CONFLICT (week_id, area, group_key, section) DO UPDATE SET "
                "issue_id=excluded.issue_id",
                [week_id, wg["area"], wg["group_key"], section, issue_id],
            )

    log.info("persistent_issue_done", **counters)
    return {"counters": counters}
