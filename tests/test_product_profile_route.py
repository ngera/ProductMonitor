"""Phase 6 tests — profile edit page + optional extras.py.

Covers:
- Scaffolded product loads even without an extras.py
- GET /products/{id}/profile renders chip editor with current facts
- POST /products/{id}/profile round-trips facts and preserves other meta
- Goals are not editable (removed from wizard); existing values preserved
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
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
def client():
    from webui.app import app
    return TestClient(app)


def test_scaffolded_product_loads_without_extras_py(products_dir):
    """New products no longer write extras.py; load must still succeed."""
    product_mod.scaffold_product("acme", "Acme", "A test")
    assert not (products_dir / "acme" / "extras.py").exists()
    spec = product_mod.load_product("acme")
    assert spec.id == "acme"
    # An empty ProductExtras class is substituted.
    assert spec.extras_cls.__name__ == "ProductExtras"


def test_scaffold_still_omits_extras_module_key_from_yaml(products_dir):
    product_mod.scaffold_product("acme", "Acme")
    meta = yaml.safe_load(
        (products_dir / "acme" / "product.yaml").read_text(encoding="utf-8")
    )
    assert "extras_module" not in meta
    assert "extras_class" not in meta


def test_profile_get_renders_facts(client, products_dir):
    product_mod.scaffold_product("acme", "Acme", "d", facts={
        "aliases": ["Acme Cloud"], "scope_in": ["sync bugs"],
        "goals": ["bugs"],
    })
    resp = client.get("/products/acme/profile")
    assert resp.status_code == 200
    assert "Acme Cloud" in resp.text
    assert "sync bugs" in resp.text
    # Shared product header + section tabs must render on the edit page.
    assert 'class="page-head"' in resp.text
    assert "Acme" in resp.text
    assert 'aria-label="Product sections"' in resp.text
    assert 'href="/products/acme/sources"' in resp.text
    # Goals removed from both wizard setup and profile edit — no checkbox
    # cluster, even when the on-disk product still has a legacy goals list.
    assert "name=\"goals\"" not in resp.text
    assert ">Goals<" not in resp.text


def test_profile_post_round_trips(client, products_dir):
    product_mod.scaffold_product("acme", "Acme", facts={
        "goals": ["bugs", "sentiment"],
    })
    # Competitors are a chip textarea (one name per line), matching the
    # wizard UX. Rich attrs are preserved for names that already exist.
    resp = client.post("/products/acme/profile", data={
        "aliases": "One\nTwo",
        "not_to_be_confused_with": "Nope",
        "competitors": "Rival",
        "scope_in": "sync",
        "scope_out": "marketing",
        # Form goals must be ignored — UI no longer collects them.
        "goals": ["churn_signals"],
        "url": "https://acme.example",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "notice=saved" in resp.headers["location"]
    from pipeline.product import clear_cache
    clear_cache()
    spec = product_mod.load_product("acme")
    assert spec.aliases == ["One", "Two"]
    assert spec.competitors == [
        {"name": "Rival", "aliases": [], "color": None, "context": ""},
    ]
    # Legacy on-disk goals preserved; posted goals discarded.
    assert set(spec.goals) == {"bugs", "sentiment"}
    assert spec.url == "https://acme.example"


def test_profile_post_rejects_overlong_aliases(client, products_dir):
    product_mod.scaffold_product("acme", "Acme")
    resp = client.post("/products/acme/profile", data={
        "aliases": "\n".join([f"a-{i}" for i in range(25)]),
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=" in resp.headers["location"]


def test_product_dashboard_shows_profile_card(client, products_dir):
    product_mod.scaffold_product("acme", "Acme", "d",
                                  facts={"aliases": ["X"], "competitors": ["Y"]})
    resp = client.get("/products/acme")
    assert resp.status_code == 200
    assert "Profile" in resp.text
    # Advanced fold present, prompts moved into it.
    assert "Advanced" in resp.text
    assert "/products/acme/prompts" in resp.text  # still linked, just under Advanced
