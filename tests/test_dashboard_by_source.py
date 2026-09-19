"""Dashboard by_source charts + per_run_totals product warehouse path."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pytest
import yaml

from pipeline import product as product_mod


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    products = tmp_path / "products"
    data = tmp_path / "data"
    products.mkdir()
    data.mkdir()

    monkeypatch.setattr("pipeline.product.PRODUCTS_DIR", products)
    monkeypatch.setattr("pipeline.features.PRODUCTS_DIR", products)
    monkeypatch.setattr("webui.app.PRODUCTS_DIR", products)
    product_mod.clear_cache()
    from pipeline import features as _f
    _f.clear_cache()

    from pipeline import config as _c
    orig = _c.app_config

    def _patched():
        cfg = dict(orig())
        paths = dict(cfg.get("paths") or {})
        paths["data_root"] = str(data)
        cfg["paths"] = paths
        return cfg

    monkeypatch.setattr(_c, "app_config", _patched)
    from webui import app as _wa
    from webui.services import runs as _runs
    from pipeline import token_usage as _tu
    monkeypatch.setattr(_wa, "app_config", _patched)
    monkeypatch.setattr(_runs, "app_config", _patched)
    # per_run_totals imports app_config inside helper — patch module binding
    monkeypatch.setattr(_c, "app_config", _patched)

    return products, data


def _scaffold_with_sources(products: Path, slug: str) -> None:
    product_mod.scaffold_product(slug, slug.title())
    sources = [
        {
            "id": "rss",
            "type": "rss",
            "display": "RSS Feeds",
            "streams": [{"name": "n", "feed_url": "https://example.com/rss"}],
        },
        {
            "id": "reddit_rss",
            "type": "reddit_rss",
            "display": "Reddit",
            "streams": [{"name": "r", "feed_url": "https://example.com/r.rss"}],
        },
    ]
    (products / slug / "sources.yaml").write_text(
        yaml.safe_dump({"sources": sources}), encoding="utf-8",
    )
    product_mod.clear_cache()


def _init_warehouse(data: Path, product_id: str) -> Path:
    wh = data / product_id / "warehouse.duckdb"
    wh.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(wh))
    con.execute(
        """CREATE TABLE items (
            id VARCHAR, source VARCHAR, source_display_name VARCHAR,
            week_id VARCHAR, filter_status VARCHAR, is_relevant BOOLEAN,
            title VARCHAR, body VARCHAR
        )"""
    )
    con.execute(
        """CREATE TABLE llm_usage (
            ts TIMESTAMP, run_id VARCHAR, stage VARCHAR, source_id VARCHAR,
            item_id VARCHAR, product_id VARCHAR, endpoint VARCHAR, model VARCHAR,
            prompt_tokens INTEGER, completion_tokens INTEGER,
            cached_input_tokens INTEGER, total_tokens INTEGER
        )"""
    )
    con.close()
    return wh


def test_per_run_totals_reads_product_warehouse_not_legacy(isolated):
    products, data = isolated
    _scaffold_with_sources(products, "acme")
    wh = _init_warehouse(data, "acme")
    con = duckdb.connect(str(wh))
    now = datetime.now(timezone.utc)
    con.execute(
        "INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [now, "run-1", "relevance", "rss", "i1", "acme",
         "http://localhost:11434/v1", "m", 10, 5, 0, 15],
    )
    con.execute(
        "INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [now, "run-1", "classify", "reddit_rss", "i2", "acme",
         "http://localhost:11434/v1", "m", 20, 10, 0, 30],
    )
    con.close()

    # Legacy global warehouse must NOT be what we read — leave it empty/absent.
    from pipeline import token_usage as tu
    pr = tu.per_run_totals("acme", "run-1")
    assert pr["total_tokens"] == 45
    assert pr["by_source"]["rss"]["tokens"] == 15
    assert pr["by_source"]["reddit_rss"]["tokens"] == 30


def test_extend_dashboard_summary_fills_volume_and_tokens(isolated):
    products, data = isolated
    _scaffold_with_sources(products, "acme")
    wh = _init_warehouse(data, "acme")
    con = duckdb.connect(str(wh))
    for i, src in enumerate(["rss", "rss", "reddit_rss"]):
        con.execute(
            "INSERT INTO items VALUES (?,?,?,?,?,?,?,?)",
            [f"id-{i}", src, src, "2026-W38", "passed", True, "t", "b"],
        )
    now = datetime.now(timezone.utc)
    con.execute(
        "INSERT INTO llm_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [now, "ui-run-1", "relevance", "rss", "id-0", "acme",
         "ep", "m", 100, 50, 0, 150],
    )
    con.close()

    logs = data / "acme" / "run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "ui-run-1.json").write_text(
        json.dumps({
            "run_id": "ui-run-1",
            "status": "success",
            "week_id": "2026-W38",
            "counters": {},
            "stage_durations": {},
        }),
        encoding="utf-8",
    )

    from webui.services.runs import product_dashboard_summary
    from webui.services import dashboard as dash

    base = product_dashboard_summary("acme")
    ext = dash.extend_dashboard_summary("acme", base)
    by = {r["source_type"]: r for r in ext["by_source"]}
    assert by["rss"]["items"] == 2
    assert by["rss"]["tokens"] == 150
    assert by["reddit_rss"]["items"] == 1
    assert by["reddit_rss"]["tokens"] == 0
