"""Initialize the DuckDB warehouse and SQLite state DBs (DESIGN.md §5.2, §5.3).

Idempotent: uses CREATE TABLE IF NOT EXISTS. Safe to re-run.

    python scripts/init_db.py                     # initializes default topic ('windows')
    python scripts/init_db.py --topic salesforce  # initializes a different topic
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb  # noqa: E402

from pipeline.config import app_config, resolve_path, set_current_topic  # noqa: E402
from pipeline.topic import DEFAULT_TOPIC, load_topic  # noqa: E402

# --- DuckDB warehouse schema (V1) -------------------------------------------

WAREHOUSE_DDL = """
CREATE TABLE IF NOT EXISTS items (
    id                   VARCHAR PRIMARY KEY,
    source               VARCHAR NOT NULL,
    source_display_name  VARCHAR NOT NULL,
    external_id          VARCHAR NOT NULL,
    url                  VARCHAR NOT NULL,
    parent_id            VARCHAR,
    author               VARCHAR,
    created_at           TIMESTAMP NOT NULL,
    fetched_at           TIMESTAMP NOT NULL,
    week_id              VARCHAR NOT NULL,
    title                VARCHAR,
    body                 TEXT NOT NULL,
    engagement_json      VARCHAR,
    raw_ref              VARCHAR,
    filter_status        VARCHAR,
    relevance_score      DOUBLE,
    is_relevant          BOOLEAN
);
CREATE INDEX IF NOT EXISTS idx_items_week ON items(week_id);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source);

-- One row per item regardless of how many areas. is_relevant lives ONLY on
-- items (authoritative); not duplicated here (§5.2 v0.4 fix).
CREATE TABLE IF NOT EXISTS item_classifications (
    item_id              VARCHAR PRIMARY KEY,
    content_types_json   VARCHAR,
    sentiment            DOUBLE,
    summary              VARCHAR,
    confidence           DOUBLE,
    primary_area         VARCHAR,
    model                VARCHAR,
    classified_at        TIMESTAMP
);

CREATE TABLE IF NOT EXISTS item_areas (
    item_id              VARCHAR NOT NULL,
    area                 VARCHAR NOT NULL,
    is_primary           BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (item_id, area)
);

CREATE TABLE IF NOT EXISTS bug_attributes (
    item_id              VARCHAR PRIMARY KEY,
    severity             VARCHAR,
    is_regression        BOOLEAN,
    reproducibility      VARCHAR,
    repro_steps_quality  VARCHAR,
    repro_steps_json     VARCHAR,
    preconditions_json   VARCHAR
);

CREATE TABLE IF NOT EXISTS request_attributes (
    item_id                       VARCHAR PRIMARY KEY,
    specificity                   VARCHAR,
    existing_workaround_mentioned BOOLEAN
);

CREATE TABLE IF NOT EXISTS item_context (
    item_id                        VARCHAR PRIMARY KEY,
    user_context                   VARCHAR,
    windows_version_major          VARCHAR,
    windows_version_feature_update VARCHAR,
    windows_version_build          VARCHAR,
    windows_version_channel        VARCHAR,
    windows_version_confidence     VARCHAR
);

-- PK includes type, NULL product handled via product_key (§5.2 v0.4 fix).
CREATE TABLE IF NOT EXISTS entity_mentions (
    item_id       VARCHAR NOT NULL,
    type          VARCHAR NOT NULL,
    vendor        VARCHAR NOT NULL,
    product_key   VARCHAR NOT NULL,
    role          VARCHAR NOT NULL,
    product       VARCHAR,
    version       VARCHAR,
    confidence    DOUBLE,
    verbatim      VARCHAR,
    PRIMARY KEY (item_id, type, vendor, product_key, role)
);
CREATE INDEX IF NOT EXISTS idx_entity_vendor ON entity_mentions(vendor);
CREATE INDEX IF NOT EXISTS idx_entity_type_vendor ON entity_mentions(type, vendor);

CREATE TABLE IF NOT EXISTS regex_extractions (
    item_id         VARCHAR PRIMARY KEY,
    kb_numbers      VARCHAR,
    cve_ids         VARCHAR,
    build_numbers   VARCHAR,
    vendor_hits     VARCHAR
);

-- Per-week groupings, keyed in the item's PRIMARY area only (§4.8).
CREATE TABLE IF NOT EXISTS week_groups (
    week_id           VARCHAR NOT NULL,
    area              VARCHAR NOT NULL,
    group_key         VARCHAR NOT NULL,
    canonical_item_id VARCHAR NOT NULL,
    member_count      INT,
    PRIMARY KEY (week_id, area, group_key)
);

CREATE TABLE IF NOT EXISTS week_group_members (
    week_id       VARCHAR NOT NULL,
    area          VARCHAR NOT NULL,
    group_key     VARCHAR NOT NULL,
    item_id       VARCHAR NOT NULL,
    is_canonical  BOOLEAN,
    PRIMARY KEY (week_id, area, group_key, item_id)
);

CREATE TABLE IF NOT EXISTS scores (
    item_id       VARCHAR PRIMARY KEY,
    score         DOUBLE,
    engagement_w  DOUBLE,
    source_w      DOUBLE,
    recency_w     DOUBLE,
    computed_at   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS weekly_rollup (
    week_id               VARCHAR NOT NULL,
    area                  VARCHAR NOT NULL,
    taxonomy_version      VARCHAR NOT NULL,
    item_count            INT,
    bug_count             INT,
    feature_request_count INT,
    feedback_count        INT,
    praise_count          INT,
    workaround_count      INT,
    avg_sentiment         DOUBLE,
    weighted_sentiment    DOUBLE,
    severity_max          VARCHAR,
    group_count           INT,
    top_group_keys_json   VARCHAR,
    top_vendors_json      VARCHAR,
    computed_at           TIMESTAMP,
    PRIMARY KEY (week_id, area)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id           VARCHAR PRIMARY KEY,
    week_id          VARCHAR,
    started_at       TIMESTAMP,
    finished_at      TIMESTAMP,
    status           VARCHAR,
    stage_durations  VARCHAR,
    counters         VARCHAR,
    completeness     VARCHAR,
    errors           VARCHAR,
    taxonomy_version VARCHAR,
    vendors_version  VARCHAR,
    code_version     VARCHAR
);
"""

STATE_DDL = """
CREATE TABLE IF NOT EXISTS seen_ids (
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS cursors (
    source      TEXT NOT NULL,
    stream      TEXT NOT NULL,
    cursor_ts   REAL,
    updated_at  TEXT,
    PRIMARY KEY (source, stream)
);
"""


def init_warehouse(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(WAREHOUSE_DDL)
    finally:
        con.close()
    print(f"[init_db] warehouse ready: {db_path}")


def init_state(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        con.executescript(STATE_DDL)
        con.commit()
    finally:
        con.close()
    print(f"[init_db] state ready: {db_path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Initialize per-topic warehouse + state DBs.")
    ap.add_argument("--topic", default=DEFAULT_TOPIC, help="Topic id under topics/.")
    args = ap.parse_args()

    topic = load_topic(args.topic)
    set_current_topic(topic)

    cfg = app_config()
    paths = cfg["paths"]
    data_root = resolve_path(paths["data_root"]) / topic.id

    init_warehouse(data_root / "warehouse.duckdb")
    init_state(data_root / "state.sqlite")

    # Ensure per-topic data dirs exist.
    for sub in ("raw", "run_logs"):
        (data_root / sub).mkdir(parents=True, exist_ok=True)
    # Reports tree.
    (resolve_path(paths["reports_root"]) / topic.id).mkdir(parents=True, exist_ok=True)

    print(f"[init_db] topic={topic.id} done.")


if __name__ == "__main__":
    main()
