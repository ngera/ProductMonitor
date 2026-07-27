"""Home page shows wizard v2 drafts alongside finished products.

Half-set-up products (in-flight wizard drafts) must not vanish between
browser sessions — the home page shows them with a Draft badge, current
step, progress bar, and Resume / Discard actions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def products_dir(tmp_path, monkeypatch):
    d = tmp_path / "products"; d.mkdir()
    monkeypatch.setattr("pipeline.product.PRODUCTS_DIR", d)
    monkeypatch.setattr("pipeline.features.PRODUCTS_DIR", d)
    monkeypatch.setattr("webui.app.PRODUCTS_DIR", d)
    from pipeline import product as _p, features as _f
    _p.clear_cache(); _f.clear_cache()
    return d


@pytest.fixture
def enable_v2(monkeypatch):
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: flag == "wizard_v2_enabled")


@pytest.fixture
def client():
    from webui.app import app
    return TestClient(app)


def _seed_real_product(products_dir: Path, slug: str = "acme") -> None:
    """Minimal product tree that load_product() accepts."""
    from pipeline.product import scaffold_product
    scaffold_product(slug, slug.capitalize(), "a live product")


def test_home_page_shows_draft_card(client, products_dir, enable_v2):
    from pipeline import wizard_v2 as wv2
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="in-progress", display="In Progress", step="profile",
        description="a product being set up",
        aliases=["Alt Name"],
        suggested_sources=[{"plugin_id": "hn", "enabled": True}],
    ))
    # Also need a real product so we don't get first-run redirect.
    _seed_real_product(products_dir)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200
    assert "In Progress" in resp.text
    # Draft badge is visible.
    assert 'class="draft-badge"' in resp.text
    # Current step surfaces so user knows where they'll land.
    assert "Screen 2 — confirm profile" in resp.text
    # Resume link points at the wizard flow.
    assert 'href="/wizard/in-progress"' in resp.text
    # Discard action is present too.
    assert "/wizard/in-progress/discard" in resp.text


def test_home_page_omits_drafts_section_when_none(client, products_dir, enable_v2):
    _seed_real_product(products_dir)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200
    # The "In progress" header shouldn't render when there are no drafts.
    assert "In progress" not in resp.text
    # The primary product is still shown.
    assert "Acme" in resp.text


def test_home_stays_on_list_when_only_drafts_exist(client, products_dir, enable_v2):
    """Historically /wizard redirected on first-run (no real products) but
    that would strand a user with an in-flight draft. If a draft exists,
    the home page must stay put so they can pick it up."""
    from pipeline import wizard_v2 as wv2
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="only-draft", display="Only Draft", step="calibrate",
    ))
    resp = client.get("/", follow_redirects=False)
    # No redirect to /wizard — user sees the draft card and can resume.
    assert resp.status_code == 200
    assert "Only Draft" in resp.text
    # And "Live products" section header does not render (no real products).
    assert "Live products" not in resp.text


def test_home_hides_drafts_section_when_flag_off(client, products_dir, monkeypatch):
    """Drafts are wizard-v2 only; when the flag is off, the section vanishes."""
    from pipeline import wizard_v2 as wv2
    wv2.save_draft(products_dir, wv2.WizardV2Draft(slug="d", display="D"))
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: False)
    _seed_real_product(products_dir)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "In progress" not in resp.text
    # But the real product is still there.
    assert "Acme" in resp.text
