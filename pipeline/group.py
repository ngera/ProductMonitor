"""Deterministic grouping — V1 "issue" model (DESIGN.md §4.8).

No embeddings. Each item joins exactly ONE issue, under its PRIMARY area
(§4.8.1), keyed by entity > KB > title-simhash (§4.8.2). Canonical item chosen
by a documented score (§4.8.3).

`primary_entity` and `choose_primary_area` live here because classify also
needs them (to persist primary_area + item_areas.is_primary).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import structlog

from pipeline import storage
from pipeline.config import (
    app_config,
    enabled_areas,
    entity_type_to_area,
)
from pipeline.models import Entity
from pipeline.util import simhash

log = structlog.get_logger()

HARDWARE_TYPES = {
    "gpu", "cpu", "chipset", "motherboard", "laptop", "desktop", "tablet",
    "audio_device", "microphone", "headphone", "speaker", "display", "webcam",
    "printer", "peripheral", "dock_hub", "external_storage", "network_adapter",
    "bluetooth_adapter",
}


# --- shared selectors (used by classify too) --------------------------------


def primary_entity(entities: list[Entity]) -> Optional[Entity]:
    """Highest-confidence feature_implicated entity; tiebreak longest verbatim,
    then earliest position (stable). DESIGN.md §4.8.2."""
    implicated = [e for e in entities if e.role == "feature_implicated"]
    if not implicated:
        return None
    return sorted(
        enumerate(implicated),
        key=lambda pair: (-pair[1].confidence, -len(pair[1].verbatim or ""), pair[0]),
    )[0][1]


def choose_primary_area(
    areas: list[str], entities: list[Entity], title: str, body: str
) -> str:
    """Pick the single area an item is grouped under (DESIGN.md §4.8.1).

    1. entity-type hint from the primary implicated entity, if it maps to a
       candidate area;
    2. else most taxonomy-keyword matches;
    3. else first candidate (declaration order).
    """
    area_defs = enabled_areas()
    declared = [a["id"] for a in area_defs]
    candidates = [a for a in areas if a in declared] or declared

    prim = primary_entity(entities)
    if prim is not None:
        hinted = entity_type_to_area().get(prim.type)
        if hinted and hinted in candidates:
            return hinted

    text = f"{title or ''} {body or ''}".lower()
    best_area = candidates[0]
    best_count = -1
    for a in area_defs:  # declaration order => stable tiebreak
        if a["id"] not in candidates:
            continue
        count = sum(1 for kw in a.get("keywords", []) if kw and kw in text)
        if count > best_count:
            best_count = count
            best_area = a["id"]
    return best_area


# --- group key (§4.8.2) ------------------------------------------------------


def compute_group_key(
    primary_area: str,
    entities: list[Entity],
    kb_numbers: list[str],
    title: str,
) -> str:
    # 1. entity key (preferred) — null product NOT eligible.
    prim = primary_entity(entities)
    if prim is not None and prim.product:
        return f"entity:{primary_area}:{prim.type}:{prim.product}"
    # 2. KB key — lowest-numbered.
    if kb_numbers:
        kb = sorted(kb_numbers)[0]
        return f"kb:{primary_area}:{kb}"
    # 3. title simhash fallback.
    return f"title:{primary_area}:{simhash(title or ''):016x}"


# --- canonical scoring (§4.8.3) ---------------------------------------------

_REPRO_QUALITY = {"detailed": 1.0, "partial": 0.5, "none": 0.0}


def canonical_score(item: dict[str, Any], max_item_score: float = 1.0) -> float:
    """Combined heuristic for choosing the "canonical" (representative)
    item within a group of duplicates.

    Prior versions of this function were quantitatively wrong on two axes:

      1. `item_score` from scores.run_score is UNBOUNDED (roughly 0.3-5
         after the engagement-floor fix in score.py). The other terms
         are normalized to [0,1]. Weights read as a convex combination
         but item_score dwarfed them — repro quality and body length
         barely influenced the pick. Fix: normalize item_score against
         the group's max before combining (caller supplies max_item_score).

      2. Engagement was double-counted — once inside item_score via
         `engagement_w = 1.0 + log1p(...)`, and again as `eng * 0.2`
         at the outer combine. Fix: drop the outer engagement term.
         Engagement's influence lives inside item_score where score.py
         already tuned its weight.

    Weights below sum to 1.0. All inputs are in [0,1] within a group.
    """
    raw = float(item.get("score") or 0.0)
    normalized_item = (raw / max_item_score) if max_item_score > 0 else 0.0
    repro = _REPRO_QUALITY.get(item.get("repro_steps_quality") or "none", 0.0)
    body_len = min(len(item.get("body") or ""), 4000) / 4000.0
    return normalized_item * 0.6 + repro * 0.3 + body_len * 0.1


# --- stage entrypoint --------------------------------------------------------


@dataclass
class _Member:
    item_id: str
    group_key: str
    area: str


def run_group(week_id: str) -> dict[str, Any]:
    """Assemble per-week groups from already-classified items.

    Reads precomputed primary_area + group inputs from the warehouse.
    """
    rows = storage.query(
        """
        SELECT i.id AS item_id,
               ic.primary_area AS primary_area,
               i.title AS title,
               i.body AS body,
               i.engagement_json AS engagement_json,
               s.score AS score,
               ba.repro_steps_quality AS repro_steps_quality,
               re.kb_numbers AS kb_numbers
        FROM items i
        JOIN item_classifications ic ON ic.item_id = i.id
        LEFT JOIN scores s ON s.item_id = i.id
        LEFT JOIN bug_attributes ba ON ba.item_id = i.id
        LEFT JOIN regex_extractions re ON re.item_id = i.id
        WHERE i.week_id = ? AND i.is_relevant = TRUE
        """,
        [week_id],
    )

    # Load ALL entities for this week's items in one query, then group by
    # item_id in Python. Prior version was N+1: `_load_entities(item_id)`
    # inside this loop opened a fresh DuckDB connection per item, each
    # under the 5.4s lock-contention ladder. On a 2,000-item week that
    # was 2,000 connections; now it's one.
    entities_by_item = _load_entities_for_items(
        [r["item_id"] for r in rows],
    )

    # Group key per item using stored entities.
    members: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        ents = entities_by_item.get(r["item_id"], [])
        kb = json.loads(r["kb_numbers"]) if r.get("kb_numbers") else []
        primary_area = r.get("primary_area") or "other"
        gk = compute_group_key(primary_area, ents, kb, r.get("title") or "")
        # `engagement_score` used to be attached here and consumed by
        # canonical_score's outer weight — but engagement is already
        # inside `item_score` via score.engagement_w, so the outer term
        # was a double-count. See canonical_score's docstring.
        members.setdefault((primary_area, gk), []).append(r)

    # Clear prior rows for idempotent re-run of this week.
    storage.execute("DELETE FROM week_groups WHERE week_id=?", [week_id])
    storage.execute("DELETE FROM week_group_members WHERE week_id=?", [week_id])

    group_rows: list[list[Any]] = []
    member_rows: list[list[Any]] = []
    for (area, gk), items in members.items():
        # Normalize item_score against the GROUP's max so canonical_score
        # is a comparable convex combination within this group. Falls
        # back to 1.0 when every item has score=0 (avoids divide-by-zero
        # and doesn't change the ordering — every normalized_item is 0).
        gmax = max((float(it.get("score") or 0.0) for it in items), default=1.0) or 1.0
        canonical = max(items, key=lambda it, m=gmax: canonical_score(it, m))
        group_rows.append([week_id, area, gk, canonical["item_id"], len(items)])
        for it in items:
            member_rows.append(
                [week_id, area, gk, it["item_id"], it["item_id"] == canonical["item_id"]]
            )

    storage.executemany(
        "INSERT INTO week_groups(week_id, area, group_key, canonical_item_id, member_count) "
        "VALUES (?,?,?,?,?)",
        group_rows,
    )
    storage.executemany(
        "INSERT INTO week_group_members(week_id, area, group_key, item_id, is_canonical) "
        "VALUES (?,?,?,?,?)",
        member_rows,
    )

    log.info("grouped", groups=len(group_rows), members=len(member_rows))
    return {"groups": len(group_rows), "members": len(member_rows)}


def _load_entities(item_id: str) -> list[Entity]:
    """Single-item convenience wrapper. New code should use the batched
    `_load_entities_for_items` — see run_group for the pattern."""
    return _load_entities_for_items([item_id]).get(item_id, [])


def _load_entities_for_items(
    item_ids: list[str],
) -> dict[str, list[Entity]]:
    """Load entity_mentions for a batch of item_ids in ONE query. Returns
    a dict keyed by item_id. Items with zero mentions are omitted from
    the dict (callers should default to [] via .get())."""
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    rows = storage.query(
        f"SELECT item_id, type, product, version, role, confidence, verbatim "
        f"FROM entity_mentions WHERE item_id IN ({placeholders})",
        list(item_ids),
    )
    out: dict[str, list[Entity]] = {}
    for r in rows:
        try:
            ent = Entity(
                type=r["type"], product=r.get("product"),
                version=r.get("version"), role=r["role"],
                confidence=r.get("confidence") if r.get("confidence") is not None else 0.5,
                verbatim=r.get("verbatim") or "",
            )
        except Exception:
            continue
        out.setdefault(r["item_id"], []).append(ent)
    return out


def _eng_score(engagement_json: Optional[str]) -> float:
    import math

    try:
        e = json.loads(engagement_json or "{}")
    except json.JSONDecodeError:
        return 0.0
    raw = int(e.get("upvotes", 0)) + 2 * int(e.get("comment_count", 0))
    return math.log1p(max(raw, 0)) / 10.0  # normalized-ish into [0,~1]
