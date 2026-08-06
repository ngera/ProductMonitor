"""One-off migration: remove the vendor concept from existing warehouses.

Drops:
  - entity_mentions.vendor (part of the PK — table is rebuilt)
  - entity_mentions indexes idx_entity_vendor + idx_entity_type_vendor
  - regex_extractions.vendor_hits
  - weekly_rollup.top_vendors_json
  - runs.vendors_version

Idempotent — each step no-ops if the column/index is already gone. Safe
to re-run.

Usage:

    python scripts/migrate_drop_vendors.py                       # default product
    python scripts/migrate_drop_vendors.py --product <slug>      # specific product
    python scripts/migrate_drop_vendors.py --all                 # every product under products/

The default product path mirrors init_db.py so this fits the same
lifecycle: fresh installs use the updated DDL directly; existing
warehouses run this script once before their next pipeline run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402

from pipeline.config import app_config, resolve_path  # noqa: E402
from pipeline.product import DEFAULT_PRODUCT, PRODUCTS_DIR, available_products  # noqa: E402


def _columns(con: "duckdb.DuckDBPyConnection", table: str) -> set[str]:
    rows = con.execute(f"PRAGMA table_info('{table}')").fetchall()
    return {r[1] for r in rows}


def _table_exists(con: "duckdb.DuckDBPyConnection", table: str) -> bool:
    row = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [table],
    ).fetchone()
    return bool(row and row[0])


def _rebuild_entity_mentions(con: "duckdb.DuckDBPyConnection") -> None:
    """DuckDB can't DROP COLUMN on a PK, so rebuild the table."""
    if not _table_exists(con, "entity_mentions"):
        return
    if "vendor" not in _columns(con, "entity_mentions"):
        return
    con.execute("DROP INDEX IF EXISTS idx_entity_vendor")
    con.execute("DROP INDEX IF EXISTS idx_entity_type_vendor")
    con.execute("""
        CREATE TABLE entity_mentions_new (
            item_id       VARCHAR NOT NULL,
            type          VARCHAR NOT NULL,
            product_key   VARCHAR NOT NULL,
            role          VARCHAR NOT NULL,
            product       VARCHAR,
            version       VARCHAR,
            confidence    DOUBLE,
            verbatim      VARCHAR,
            PRIMARY KEY (item_id, type, product_key, role)
        )
    """)
    # Existing rows may have duplicate PKs across different vendors —
    # collapse them by picking one row per new-PK. Highest confidence wins.
    con.execute("""
        INSERT INTO entity_mentions_new
        SELECT item_id, type, product_key, role, product, version, confidence, verbatim
        FROM (
            SELECT item_id, type, product_key, role, product, version, confidence, verbatim,
                   ROW_NUMBER() OVER (
                       PARTITION BY item_id, type, product_key, role
                       ORDER BY confidence DESC NULLS LAST
                   ) AS rn
            FROM entity_mentions
        )
        WHERE rn = 1
    """)
    con.execute("DROP TABLE entity_mentions")
    con.execute("ALTER TABLE entity_mentions_new RENAME TO entity_mentions")
    print("  entity_mentions: vendor column dropped (table rebuilt)")


def _drop_column(
    con: "duckdb.DuckDBPyConnection", table: str, column: str,
) -> None:
    if not _table_exists(con, table):
        return
    if column not in _columns(con, table):
        return
    con.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    print(f"  {table}.{column}: dropped")


def migrate_warehouse(db_path: Path) -> None:
    if not db_path.exists():
        print(f"[migrate] warehouse not found, skipping: {db_path}")
        return
    print(f"[migrate] {db_path}")
    con = duckdb.connect(str(db_path))
    try:
        _rebuild_entity_mentions(con)
        _drop_column(con, "regex_extractions", "vendor_hits")
        _drop_column(con, "weekly_rollup", "top_vendors_json")
        _drop_column(con, "runs", "vendors_version")
    finally:
        con.close()
    print(f"[migrate] done: {db_path}")


def _warehouse_path(product_id: str) -> Path:
    cfg = app_config()
    data_root = resolve_path(cfg["paths"]["data_root"]) / product_id
    return data_root / "warehouse.duckdb"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--product", default=None, help="Product slug (default: %s)" % DEFAULT_PRODUCT)
    ap.add_argument("--all", action="store_true", help="Migrate every product under products/")
    args = ap.parse_args()

    if args.all:
        if not PRODUCTS_DIR.exists():
            print(f"[migrate] no products directory at {PRODUCTS_DIR}")
            return
        products = available_products()
        if not products:
            print("[migrate] no products found")
            return
        for pid in products:
            migrate_warehouse(_warehouse_path(pid))
    else:
        pid = args.product or DEFAULT_PRODUCT
        migrate_warehouse(_warehouse_path(pid))


if __name__ == "__main__":
    main()
