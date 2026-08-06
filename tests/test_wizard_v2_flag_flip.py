"""Phase 7 tests — flag flip + / -> /wizard redirect.

Covers:
- With wizard_v2_enabled=True and only demo/no products, / redirects to /wizard
- With non-demo products present, / does NOT redirect
- With wizard_v2_enabled=False, / renders the legacy first-run screen
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


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
def client():
    from webui.app import app
    return TestClient(app)


def _seed_demo(products_dir: Path) -> None:
    """Create a minimal 'demo' product so available_products() finds it."""
    d = products_dir / "demo"
    (d / "examples" / "positive").mkdir(parents=True)
    (d / "examples" / "negative").mkdir(parents=True)
    (d / "product.yaml").write_text("id: demo\ndisplay: Demo\ndescription: d\nschedule: weekly\n", encoding="utf-8")
    (d / "taxonomy.yaml").write_text(
        "version: '2026-01-01'\nareas:\n  - id: g\n    display: G\n    enabled: true\n"
        "    features:\n      - id: g\n        display: G\n        description: G\n",
        encoding="utf-8",
    )
    (d / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    (d / "prompts.yaml").write_text(
        "relevance:\n  system: s\n  template: t\nclassify:\n  system: s\n  template: t\n",
        encoding="utf-8",
    )
    (d / "llm_routing.yaml").write_text(
        "relevance:\n  endpoint: http://x\n  model: m\n"
        "classify:\n  endpoint: http://x\n  model: m\n",
        encoding="utf-8",
    )


def test_index_redirects_to_wizard_when_only_demo_and_v2_on(client, products_dir, monkeypatch):
    _seed_demo(products_dir)
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: flag == "wizard_v2_enabled")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/wizard"


def test_index_does_not_redirect_when_non_demo_products_exist(client, products_dir, monkeypatch):
    _seed_demo(products_dir)
    # Add a second non-demo product.
    from pipeline.product import scaffold_product
    scaffold_product("acme", "Acme")
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: flag == "wizard_v2_enabled")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200


def test_index_does_not_redirect_when_v2_flag_off(client, products_dir, monkeypatch):
    _seed_demo(products_dir)
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: flag == "wizard_enabled")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200
