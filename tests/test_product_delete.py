"""Delete-product route tests.

Removes products/<id>/ only. Reports (reports_root), run logs
(run_logs_root), and warehouse data (data_root) must remain intact so old
report URLs keep resolving.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline import product as product_mod


@pytest.fixture
def products_dir(tmp_path, monkeypatch):
    d = tmp_path / "products"
    d.mkdir()
    monkeypatch.setattr("pipeline.product.PRODUCTS_DIR", d)
    monkeypatch.setattr("pipeline.features.PRODUCTS_DIR", d)
    monkeypatch.setattr("webui.app.PRODUCTS_DIR", d)
    from pipeline import product as _p, features as _f
    _p.clear_cache(); _f.clear_cache()
    return d


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """Stub app_config so the reports/data/logs dirs live under tmp_path."""
    def _fake_app_config():
        return {"paths": {
            "data_root": str(tmp_path / "data"),
            "reports_root": str(tmp_path / "reports"),
            "run_logs_root": str(tmp_path / "logs"),
            "raw_root": str(tmp_path / "raw"),
            "warehouse_db": str(tmp_path / "wh.duckdb"),
            "state_db": str(tmp_path / "state.sqlite"),
        }}
    monkeypatch.setattr("pipeline.config.app_config", _fake_app_config)
    monkeypatch.setattr("webui.app.app_config", _fake_app_config)
    return tmp_path


@pytest.fixture
def client():
    from webui.app import app
    return TestClient(app)


def test_delete_removes_product_dir_and_keeps_reports(client, products_dir,
                                                      isolated_data):
    product_mod.scaffold_product("acme", "Acme", "Test")
    # Sibling directories that must survive the delete.
    (isolated_data / "reports" / "acme").mkdir(parents=True)
    (isolated_data / "reports" / "acme" / "2026-07-25.html").write_text(
        "old report", encoding="utf-8",
    )
    (isolated_data / "logs" / "acme").mkdir(parents=True)
    (isolated_data / "logs" / "acme" / "run-1.json").write_text(
        "{}", encoding="utf-8",
    )
    (isolated_data / "data" / "acme").mkdir(parents=True)
    (isolated_data / "data" / "acme" / "cursor.state").write_text(
        "x", encoding="utf-8",
    )

    resp = client.post("/products/acme/delete",
                       data={"confirm_slug": "acme"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "/?notice=deleted" in resp.headers["location"]

    # Config gone.
    assert not (products_dir / "acme").exists()
    # Reports / logs / data preserved.
    assert (isolated_data / "reports" / "acme" / "2026-07-25.html").exists()
    assert (isolated_data / "logs" / "acme" / "run-1.json").exists()
    assert (isolated_data / "data" / "acme" / "cursor.state").exists()


def test_delete_rejects_wrong_confirm_slug(client, products_dir, isolated_data):
    product_mod.scaffold_product("acme", "Acme")
    resp = client.post("/products/acme/delete",
                       data={"confirm_slug": "wrong-name"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "error=confirmation+did+not+match" in resp.headers["location"]
    # Config still there.
    assert (products_dir / "acme" / "product.yaml").exists()


def test_delete_returns_404_for_missing_product(client, products_dir, isolated_data):
    resp = client.post("/products/does-not-exist/delete",
                       data={"confirm_slug": "does-not-exist"})
    assert resp.status_code == 404


def test_product_page_shows_danger_zone(client, products_dir, isolated_data):
    product_mod.scaffold_product("acme", "Acme")
    resp = client.get("/products/acme")
    assert resp.status_code == 200
    assert "Danger zone" in resp.text
    assert "Delete product configuration" in resp.text
    assert 'name="confirm_slug"' in resp.text
