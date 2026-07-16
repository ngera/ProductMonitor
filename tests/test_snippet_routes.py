"""End-to-end webui tests for snippet workflow paths B and C (§4.4).

Exercises the FastAPI routes with:
  - A fixture product on disk (real product loader, real YAML files)
  - Feature flags controlled via a per-test features.yaml
  - Warehouse queries stubbed at the storage layer

Verifies:
  - GET /snippets/candidates renders "off" banner when flag is off
  - POST /snippets/candidates/decide creates a snippet with created_at
  - POST /runs/{run_id}/review/to-snippet 403s when flag is off
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixture product
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_product(tmp_path, monkeypatch):
    """Create a minimal on-disk product so load_product() succeeds."""
    products_dir = tmp_path / "products"
    product_id = "test_prod"
    pdir = products_dir / product_id
    (pdir / "examples" / "positive").mkdir(parents=True)
    (pdir / "examples" / "negative").mkdir(parents=True)
    (pdir / "product.yaml").write_text(
        "id: test_prod\ndisplay: Test\ndescription: t\n",
        encoding="utf-8",
    )
    (pdir / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    (pdir / "taxonomy.yaml").write_text(
        yaml.safe_dump({
            "areas": [
                {"id": "audio", "display": "Audio", "features": [{"id": "output", "display": "Output"}], "enabled": True},
                {"id": "video", "display": "Video", "features": [{"id": "codec", "display": "Codec"}], "enabled": True},
            ],
        }),
        encoding="utf-8",
    )
    (pdir / "vendors.yaml").write_text("vendors: []\n", encoding="utf-8")
    (pdir / "prompts.yaml").write_text("relevance: ''\nclassify: ''\n", encoding="utf-8")
    (pdir / "llm_routing.yaml").write_text("{}\n", encoding="utf-8")
    (pdir / "extras.py").write_text(
        "from pydantic import BaseModel\n\nclass ProductExtras(BaseModel):\n    pass\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("pipeline.product.PRODUCTS_DIR", products_dir)
    monkeypatch.setattr("pipeline.features.PRODUCTS_DIR", products_dir)
    monkeypatch.setattr("webui.app.PRODUCTS_DIR", products_dir)

    # Isolate data root so temp_runs writes go into tmp_path. Patch both the
    # origin (pipeline.config) AND the alias imported into webui.app.
    def _fake_app_config():
        return {"paths": {"data_root": str(tmp_path / "data"),
                          "reports_root": str(tmp_path / "reports"),
                          "raw_root": str(tmp_path / "raw"),
                          "warehouse_db": str(tmp_path / "wh.duckdb"),
                          "state_db": str(tmp_path / "state.sqlite"),
                          "run_logs_root": str(tmp_path / "logs")}}
    monkeypatch.setattr("pipeline.config.app_config", _fake_app_config)
    monkeypatch.setattr("webui.app.app_config", _fake_app_config)

    # Clear caches so the loader picks up the fresh path
    from pipeline.product import clear_cache
    from pipeline.features import clear_cache as clear_features
    clear_cache()
    clear_features()

    return product_id, pdir, tmp_path


@pytest.fixture
def client():
    from webui.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Path B: /snippets/candidates
# ---------------------------------------------------------------------------


def test_candidates_page_shows_flag_off_banner(client, fixture_product, monkeypatch):
    """With snippet_candidates_enabled=False, we show a banner (200 OK)."""
    product_id, _, _ = fixture_product
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: False)
    resp = client.get(f"/products/{product_id}/snippets/candidates")
    assert resp.status_code == 200
    # Off banner mentions the flag name and points at /admin/features.
    assert "snippet_candidates_enabled" in resp.text
    assert "/admin/features" in resp.text


def test_candidates_decide_writes_snippet(client, fixture_product, monkeypatch, tmp_path):
    """A candidate cache exists → POST decide writes a snippet YAML."""
    product_id, pdir, root = fixture_product
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: True)

    # Seed a candidate cache on disk (same structure the route writes)
    nonce = "abcd1234"
    cache = {
        "nonce": nonce,
        "product_id": product_id,
        "total_relevant": 100,
        "per_area_counts": {"audio": 3},
        "candidates": [
            {
                "item": {
                    "id": "item_xyz",
                    "source_display_name": "reddit",
                    "title": "A helpful title",
                    "body": "This is the body text.",
                    "url": "https://example.com/x",
                    "author": "someone",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "primary_area": "audio",
                    "summary": "",
                    "sentiment": -0.3,
                },
                "suggestion": {
                    "item_id": "item_xyz",
                    "polarity": "positive_example",
                    "why": "Clear audio bug report",
                },
                "decided": None,
            },
        ],
    }
    cache_path = root / "data" / product_id / "temp_runs" / f"candidates_{nonce}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    resp = client.post(
        f"/products/{product_id}/snippets/candidates/decide",
        data={"nonce": nonce, "idx": "0", "decision": "positive_example"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"idx=1" in resp.headers["location"]

    # Snippet YAML created
    written = list((pdir / "examples" / "positive").glob("*.yaml"))
    assert len(written) == 1
    blob = yaml.safe_load(written[0].read_text(encoding="utf-8"))
    assert blob["polarity"] == "positive_example"
    assert blob["body"] == "This is the body text."


def test_candidates_decide_skip_advances_without_snippet(client, fixture_product, monkeypatch, tmp_path):
    product_id, pdir, root = fixture_product
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: True)

    nonce = "efgh5678"
    cache = {
        "nonce": nonce, "product_id": product_id,
        "total_relevant": 1, "per_area_counts": {"video": 1},
        "candidates": [{
            "item": {"id": "skip_me", "source_display_name": "", "title": "",
                     "body": "text", "url": "", "author": "",
                     "created_at": "", "primary_area": "video",
                     "summary": "", "sentiment": 0},
            "suggestion": {"item_id": "skip_me", "polarity": "positive_example", "why": ""},
            "decided": None,
        }],
    }
    cache_path = root / "data" / product_id / "temp_runs" / f"candidates_{nonce}.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(json.dumps(cache), encoding="utf-8")

    resp = client.post(
        f"/products/{product_id}/snippets/candidates/decide",
        data={"nonce": nonce, "idx": "0", "decision": "skip"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert not list((pdir / "examples" / "positive").glob("*.yaml"))
    assert not list((pdir / "examples" / "negative").glob("*.yaml"))


# ---------------------------------------------------------------------------
# Path C: to-snippet from run review
# ---------------------------------------------------------------------------


def test_review_to_snippet_403_when_flag_off(client, fixture_product, monkeypatch):
    product_id, _, _ = fixture_product
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: False)

    resp = client.post(
        f"/products/{product_id}/runs/some_run/review/to-snippet",
        data={"polarity": "positive_example", "item_id": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_review_to_snippet_400_on_invalid_polarity(client, fixture_product, monkeypatch):
    product_id, _, _ = fixture_product
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: True)

    resp = client.post(
        f"/products/{product_id}/runs/some_run/review/to-snippet",
        data={"polarity": "bogus", "item_id": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
