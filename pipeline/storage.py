"""Storage layer: DuckDB warehouse + SQLite state (DESIGN.md §5).

Raw JSONL is the system of record; this module manages the derived warehouse
and the small append-only state DB (seen_ids, cursors).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import duckdb

from pipeline.config import app_config, resolve_path


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _product_id_or_none() -> str | None:
    """Best-effort: return the current product id, or None for the legacy
    single-product path layout."""
    try:
        from pipeline.config import current_product
        return current_product().id
    except Exception:
        return None


def warehouse_path() -> Path:
    """Per-product warehouse: data/<product_id>/warehouse.duckdb.
    Falls back to the legacy app.yaml `warehouse_db` path when no product
    is loaded (preserves behaviour for callers like scripts/init_db.py used
    directly)."""
    product_id = _product_id_or_none()
    if product_id is None:
        return resolve_path(app_config()["paths"]["warehouse_db"])
    return resolve_path(app_config()["paths"]["data_root"]) / product_id / "warehouse.duckdb"


def state_path() -> Path:
    """Per-product state: data/<product_id>/state.sqlite. Legacy fallback as above."""
    product_id = _product_id_or_none()
    if product_id is None:
        return resolve_path(app_config()["paths"]["state_db"])
    return resolve_path(app_config()["paths"]["data_root"]) / product_id / "state.sqlite"


@contextmanager
def warehouse() -> Iterator[duckdb.DuckDBPyConnection]:
    con = duckdb.connect(str(warehouse_path()))
    try:
        yield con
    finally:
        con.close()


@contextmanager
def state() -> Iterator[sqlite3.Connection]:
    con = sqlite3.connect(str(state_path()))
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


# --- State: seen_ids & cursors ----------------------------------------------


def filter_unseen(source: str, external_ids: Iterable[str]) -> set[str]:
    """Return the subset of external_ids not already in seen_ids."""
    ids = list(external_ids)
    if not ids:
        return set()
    with state() as con:
        placeholders = ",".join("?" * len(ids))
        rows = con.execute(
            f"SELECT external_id FROM seen_ids WHERE source=? AND external_id IN ({placeholders})",
            [source, *ids],
        ).fetchall()
        seen = {r["external_id"] for r in rows}
    return {i for i in ids if i not in seen}


def mark_seen(source: str, external_ids: Iterable[str]) -> None:
    ts = _now().isoformat()
    rows = [(source, eid, ts) for eid in external_ids]
    if not rows:
        return
    with state() as con:
        con.executemany(
            "INSERT OR IGNORE INTO seen_ids(source, external_id, first_seen) VALUES (?,?,?)",
            rows,
        )
        con.commit()


def get_cursor(source: str, stream: str) -> Optional[float]:
    with state() as con:
        row = con.execute(
            "SELECT cursor_ts FROM cursors WHERE source=? AND stream=?",
            [source, stream],
        ).fetchone()
    return row["cursor_ts"] if row else None


def set_cursor(source: str, stream: str, cursor_ts: float) -> None:
    with state() as con:
        con.execute(
            """INSERT INTO cursors(source, stream, cursor_ts, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(source, stream) DO UPDATE SET cursor_ts=excluded.cursor_ts,
                                                         updated_at=excluded.updated_at""",
            [source, stream, cursor_ts, _now().isoformat()],
        )
        con.commit()


# --- Warehouse: items --------------------------------------------------------


def upsert_items(rows: list[dict[str, Any]]) -> int:
    """Upsert normalized items keyed on id ({source}:{external_id})."""
    if not rows:
        return 0
    cols = [
        "id", "source", "source_display_name", "external_id", "url", "parent_id",
        "author", "created_at", "fetched_at", "week_id", "title", "body",
        "engagement_json", "raw_ref", "filter_status", "relevance_score", "is_relevant",
        "is_reply", "author_intent",
    ]
    placeholders = ",".join("?" * len(cols))
    # DuckDB disallows updating PK/indexed columns in ON CONFLICT; these are
    # immutable for a given id anyway (id PK, source/week_id indexed).
    immutable = {"id", "source", "week_id"}
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in immutable)
    with warehouse() as con:
        con.executemany(
            f"INSERT INTO items ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            [[r.get(c) for c in cols] for r in rows],
        )
    return len(rows)


def set_filter_status(item_id: str, status: str) -> None:
    with warehouse() as con:
        con.execute("UPDATE items SET filter_status=? WHERE id=?", [status, item_id])


def set_relevance(item_id: str, score: float, is_relevant: bool) -> None:
    with warehouse() as con:
        con.execute(
            "UPDATE items SET relevance_score=?, is_relevant=? WHERE id=?",
            [score, is_relevant, item_id],
        )


def items_for_week(week_id: str, filter_status: Optional[str] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM items WHERE week_id=?"
    params: list[Any] = [week_id]
    if filter_status:
        sql += " AND filter_status=?"
        params.append(filter_status)
    with warehouse() as con:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def query(sql: str, params: Optional[list[Any]] = None) -> list[dict[str, Any]]:
    with warehouse() as con:
        cur = con.execute(sql, params or [])
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def execute(sql: str, params: Optional[list[Any]] = None) -> None:
    with warehouse() as con:
        con.execute(sql, params or [])


def executemany(sql: str, rows: list[list[Any]]) -> None:
    if not rows:
        return
    with warehouse() as con:
        con.executemany(sql, rows)


# --- Runs --------------------------------------------------------------------


def start_run(run_id: str, week_id: str, versions: dict[str, str]) -> None:
    with warehouse() as con:
        con.execute(
            """INSERT INTO runs(run_id, week_id, started_at, status, taxonomy_version,
                                vendors_version, code_version)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (run_id) DO UPDATE SET started_at=excluded.started_at,
                                                  status=excluded.status""",
            [
                run_id, week_id, _now(), "running",
                versions.get("taxonomy", ""), versions.get("vendors", ""),
                versions.get("code", ""),
            ],
        )


def finish_run(
    run_id: str,
    status: str,
    stage_durations: dict[str, float],
    counters: dict[str, Any],
    completeness: dict[str, Any],
    errors: list[str],
) -> None:
    with warehouse() as con:
        con.execute(
            """UPDATE runs SET finished_at=?, status=?, stage_durations=?, counters=?,
                               completeness=?, errors=? WHERE run_id=?""",
            [
                _now(), status, json.dumps(stage_durations), json.dumps(counters),
                json.dumps(completeness), json.dumps(errors), run_id,
            ],
        )
