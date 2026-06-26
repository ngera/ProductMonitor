"""Normalize stage (DESIGN.md §4.4.1).

Raw JSONL -> items table. Deterministic, idempotent (upsert by id).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from pipeline import storage
from pipeline.config import app_config, resolve_path
from pipeline.util import read_jsonl, week_id_for

log = structlog.get_logger()


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def run_normalize(week_id: str) -> dict[str, Any]:
    app = app_config()
    raw_root = resolve_path(app["paths"]["raw_root"])
    fetched_at = datetime.now().astimezone()

    rows: list[dict[str, Any]] = []
    week_dirs = list(raw_root.glob(f"*/{week_id}"))
    for wd in week_dirs:
        for jsonl in wd.glob("*.jsonl"):
            for rec in read_jsonl(jsonl):
                rows.append(_to_item_row(rec, jsonl, fetched_at))

    n = storage.upsert_items(rows)
    log.info("normalized", items=n)
    return {"normalized": n}


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
    }
