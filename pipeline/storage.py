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
    """Open the current product's warehouse for the duration of the block.

    Retries on `IO Error: Could not set lock` so brief cross-process
    contention (webui reading while a pipeline subprocess writes, or
    vice-versa) doesn't crash the caller. DuckDB uses file-level locks:
    a shared/exclusive lock held by ANY process blocks a conflicting
    acquisition by another. In this project the webui (PID 1) and the
    pipeline subprocess routinely touch the same warehouse.duckdb, so
    transient contention on the order of milliseconds is normal.
    """
    import time as _time
    delays = [0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0]   # ~5.4s total budget
    last_err: Exception | None = None
    for delay in [0.0, *delays]:
        if delay:
            _time.sleep(delay)
        try:
            con = duckdb.connect(str(warehouse_path()))
            break
        except duckdb.IOException as e:
            # Only retry the "conflicting lock" flavor of IOException —
            # a bad path / permission error should fail fast.
            if "lock" not in str(e).lower():
                raise
            last_err = e
            continue
    else:
        # Loop exhausted without break — give up with the last error.
        raise last_err if last_err else RuntimeError("warehouse lock exhausted")
    try:
        yield con
    finally:
        con.close()


@contextmanager
def state() -> Iterator[sqlite3.Connection]:
    # Under ADR-0023 concurrent fetch, up to `max_concurrent_streams`
    # threads open their own SQLite connections and write to seen_ids /
    # cursors in parallel. Two knobs make this safe:
    #   - WAL mode (set once at init_db time, see scripts/init_db.init_state):
    #     lets readers and one writer proceed concurrently; concurrent
    #     writers still serialize but through the WAL rather than the
    #     rollback journal, which is faster and less error-prone.
    #   - explicit timeout=30s so a burst of concurrent writers doesn't
    #     hit SQLite's default 5s and raise OperationalError under load.
    con = sqlite3.connect(str(state_path()), timeout=30.0)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


def ensure_schema() -> None:
    """Idempotent: create the per-product warehouse + state schemas if absent.

    Pulls the canonical DDL from scripts/init_db.py so we don't drift. Called
    at run-start from pipeline.run; the CLI script does the same thing on the
    command line. Safe to call repeatedly.
    """
    # Lazy import: scripts/ isn't a package, so we load init_db by file path.
    import importlib.util
    init_db_path = Path(__file__).resolve().parent.parent / "scripts" / "init_db.py"
    spec = importlib.util.spec_from_file_location("_init_db", init_db_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    wh_path = warehouse_path()
    st_path = state_path()
    wh_path.parent.mkdir(parents=True, exist_ok=True)
    mod.init_warehouse(wh_path)
    mod.init_state(st_path)


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
        "is_reply", "author_intent", "canonical_url",
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


def set_relevance_batch(rows: list[tuple[str, float, bool]]) -> None:
    """Batched form of `set_relevance` — one warehouse connection for N updates.

    Rows are (item_id, score, is_relevant). No-op on empty input.

    Used by relevance + classify to flush accumulated per-item decisions
    together instead of opening one connection per item. On a 2,000-item
    week that's the difference between ~2,000 lock-contended open/close
    cycles and ~20 (one per flush batch).
    """
    if not rows:
        return
    with warehouse() as con:
        con.executemany(
            "UPDATE items SET relevance_score=?, is_relevant=? WHERE id=?",
            [[score, is_relevant, item_id] for (item_id, score, is_relevant) in rows],
        )


def set_filter_status_batch(rows: list[tuple[str, str]]) -> None:
    """Batched form of `set_filter_status`. Rows are (item_id, status)."""
    if not rows:
        return
    with warehouse() as con:
        con.executemany(
            "UPDATE items SET filter_status=? WHERE id=?",
            [[status, item_id] for (item_id, status) in rows],
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
                                code_version)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT (run_id) DO UPDATE SET started_at=excluded.started_at,
                                                  status=excluded.status""",
            [
                run_id, week_id, _now(), "running",
                versions.get("taxonomy", ""),
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
