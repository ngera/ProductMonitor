"""Tests for /healthz (issue #12b — unattended-install liveness signal).

Uses TestClient with an isolated products dir + fake run_logs so we can
assert age/staleness semantics without spinning up a real pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("structlog")

from fastapi.testclient import TestClient


@pytest.fixture
def isolated_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point every product/data path at a tmp tree so tests don't stomp
    the repo's real state, and reset per-product caches."""
    products = tmp_path / "products"
    data = tmp_path / "data"
    products.mkdir()
    data.mkdir()

    monkeypatch.setattr("pipeline.product.PRODUCTS_DIR", products)
    monkeypatch.setattr("pipeline.features.PRODUCTS_DIR", products)
    from pipeline import product as _p, features as _f
    _p.clear_cache()
    _f.clear_cache()

    # Rewrite app_config's data_root so _run_logs_dir points into tmp_path.
    from pipeline import config as _c
    orig = _c.app_config

    def _patched():
        cfg = orig()
        cfg = dict(cfg)
        paths = dict(cfg.get("paths") or {})
        paths["data_root"] = str(data)
        cfg["paths"] = paths
        return cfg
    monkeypatch.setattr(_c, "app_config", _patched)
    from webui import app as _wa
    monkeypatch.setattr(_wa, "app_config", _patched)

    return products, data


def _make_product(products_dir: Path, slug: str) -> None:
    (products_dir / slug).mkdir()
    (products_dir / slug / "product.yaml").write_text(
        f"id: {slug}\nname: {slug}\n", encoding="utf-8",
    )


def _write_run_log(
    data_dir: Path, product: str, run_id: str, status: str,
) -> None:
    d = data_dir / product / "run_logs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "status": status, "week_id": "2026-W37"}),
        encoding="utf-8",
    )


def test_healthz_ok_when_no_products(isolated_layout) -> None:
    from webui.app import app
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["products"] == 0
    assert body["last_run_age_seconds"] is None


def test_healthz_ok_when_recent_success(isolated_layout) -> None:
    products, data = isolated_layout
    _make_product(products, "windows")
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=2)).strftime("%Y%m%dT%H%M%S")
    _write_run_log(data, "windows", f"ui-{recent}-abc123", "success")

    from webui.app import app
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["last_run_age_seconds"] is not None
    assert 0 < body["last_run_age_seconds"] < 24 * 3600


def test_healthz_stale_when_last_success_too_old(isolated_layout) -> None:
    products, data = isolated_layout
    _make_product(products, "windows")
    # 30 days ago — well past the default 8-day threshold.
    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y%m%dT%H%M%S")
    _write_run_log(data, "windows", f"ui-{old}-abc123", "success")

    from webui.app import app
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["status"] == "stale"


def test_healthz_stale_when_only_failures(isolated_layout) -> None:
    products, data = isolated_layout
    _make_product(products, "windows")
    now = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y%m%dT%H%M%S")
    _write_run_log(data, "windows", f"ui-{now}-abc123", "failed")

    from webui.app import app
    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "stale"
    assert body["last_run_age_seconds"] is None


def test_healthz_uses_freshest_across_products(isolated_layout) -> None:
    products, data = isolated_layout
    _make_product(products, "old-product")
    _make_product(products, "fresh-product")
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=30)).strftime("%Y%m%dT%H%M%S")
    recent = (now - timedelta(hours=3)).strftime("%Y%m%dT%H%M%S")
    _write_run_log(data, "old-product",   f"ui-{old}-a",    "success")
    _write_run_log(data, "fresh-product", f"ui-{recent}-b", "success")

    from webui.app import app
    with TestClient(app) as client:
        r = client.get("/healthz")
    # Fresh product keeps the whole install healthy.
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
