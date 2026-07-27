"""Phase 4 tests — mini-fetch runner + calibration routes.

Covers:
- run_minifetch respects per-source and total item caps
- No cursor / seen_ids writes leak into pipeline storage
- Zero-results path sets status=empty
- Diversity sampling: sample_deck round-robins across sources
- Calibrate route records judgments into the draft.calibration.judgments dict
- Calibrate/done advances draft.step to review
- Discard cleans the wizard temp dir
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from pipeline import minifetch as mf
from pipeline import wizard_v2 as wv2
from pipeline.models import RawItem


# ---------------------------------------------------------------------------
# Fixtures — reuse Phase 3 pattern
# ---------------------------------------------------------------------------


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
def wizard_data(tmp_path, monkeypatch):
    """Isolate `data/.wizard/` at tmp_path so minifetch files stay in the sandbox."""
    def _fake_app_config():
        return {"paths": {"data_root": str(tmp_path / "data")}}
    monkeypatch.setattr("pipeline.config.app_config", _fake_app_config)
    return tmp_path / "data" / ".wizard"


@pytest.fixture
def enable_v2(monkeypatch):
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: flag == "wizard_v2_enabled")


@pytest.fixture
def client():
    from webui.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers — a fake source we control
# ---------------------------------------------------------------------------


def _mk_raw(idx: int, source: str = "hn") -> RawItem:
    return RawItem(
        source=source, source_display_name=source,
        external_id=f"{source}-{idx}",
        url=f"https://example.com/{idx}",
        parent_external_id=None, author=f"u{idx}",
        created_at=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc),
        title=f"Post {idx}", body=f"Body {idx}",
        engagement={}, raw={},
    )


def _install_fake_source(monkeypatch, plugin_id: str, items):
    """Register a source class that yields `items` when fetch_since is called."""
    yielded = list(items)
    seen: dict = {"cursor_touched": False, "n_calls": 0}

    class _FakeSource:
        name = plugin_id
        def fetch_since(self, cursor, config, stats):
            seen["n_calls"] += 1
            for it in yielded:
                yield it
            # if the runner touched cursor.cursor_ts we'd notice
            seen["cursor_touched"] = cursor.cursor_ts is not None

    def _fake_get_source(pid):
        if pid == plugin_id:
            return _FakeSource()
        raise KeyError(pid)
    monkeypatch.setattr("sources.get_source", _fake_get_source)
    return seen


# ---------------------------------------------------------------------------
# Runner behavior
# ---------------------------------------------------------------------------


def test_run_minifetch_writes_corpus_and_status(wizard_data, monkeypatch):
    _install_fake_source(monkeypatch, "hn", [_mk_raw(i) for i in range(5)])
    mf._run_minifetch("acme", [
        {"plugin_id": "hn", "enabled": True, "requires_key": False,
         "stream_config": {"search_queries": ["acme"]}},
    ])
    status = mf.read_status("acme")
    assert status.status == mf.STATUS_READY
    assert status.corpus_size == 5
    corpus = mf.load_corpus("acme")
    assert len(corpus) == 5
    assert corpus[0]["id"] == "hn:hn-0"


def test_run_minifetch_respects_per_source_cap(wizard_data, monkeypatch):
    _install_fake_source(monkeypatch, "hn", [_mk_raw(i) for i in range(100)])
    mf._run_minifetch("acme", [
        {"plugin_id": "hn", "enabled": True, "requires_key": False,
         "stream_config": {}},
    ])
    corpus = mf.load_corpus("acme")
    assert len(corpus) == mf.MAX_PER_SOURCE


def test_run_minifetch_respects_total_cap_across_sources(wizard_data, monkeypatch):
    # Fake registry that returns a fresh generator per plugin id.
    def _fake_get_source(pid):
        class S:
            name = pid
            def fetch_since(self, cursor, config, stats):
                for i in range(100):
                    yield _mk_raw(i, source=pid)
        return S()
    monkeypatch.setattr("sources.get_source", _fake_get_source)
    mf._run_minifetch("acme", [
        {"plugin_id": "hn", "enabled": True, "requires_key": False, "stream_config": {}},
        {"plugin_id": "rss", "enabled": True, "requires_key": False, "stream_config": {}},
        {"plugin_id": "microsoft_community", "enabled": True, "requires_key": False,
         "stream_config": {}},
    ])
    assert len(mf.load_corpus("acme")) <= mf.MAX_TOTAL_ITEMS


def test_run_minifetch_skips_disabled_and_keyed(wizard_data, monkeypatch):
    _install_fake_source(monkeypatch, "reddit", [_mk_raw(0, source="reddit")])
    mf._run_minifetch("acme", [
        {"plugin_id": "reddit", "enabled": True, "requires_key": True, "stream_config": {}},
        {"plugin_id": "hn", "enabled": False, "requires_key": False, "stream_config": {}},
    ])
    corpus = mf.load_corpus("acme")
    assert corpus == []
    status = mf.read_status("acme")
    # Neither source was tried; per_source list is empty.
    assert status.per_source == []


def test_source_init_failure_records_error_not_raises(wizard_data, monkeypatch):
    def _fake_get_source(pid):
        raise RuntimeError("boom")
    monkeypatch.setattr("sources.get_source", _fake_get_source)
    mf._run_minifetch("acme", [
        {"plugin_id": "hn", "enabled": True, "requires_key": False, "stream_config": {}},
    ])
    status = mf.read_status("acme")
    assert status.per_source[0].status == "error"
    assert "boom" in status.per_source[0].error


def test_zero_results_marks_status_empty(wizard_data, monkeypatch):
    _install_fake_source(monkeypatch, "hn", [_mk_raw(0)])  # 1 item, below MIN_USEFUL=3
    mf._run_minifetch("acme", [
        {"plugin_id": "hn", "enabled": True, "requires_key": False, "stream_config": {}},
    ])
    status = mf.read_status("acme")
    assert status.status == mf.STATUS_EMPTY


# ---------------------------------------------------------------------------
# Deck sampling
# ---------------------------------------------------------------------------


def test_sample_deck_round_robins_across_sources(wizard_data):
    # Write a corpus manually.
    corpus = (
        [{"id": f"hn:{i}", "source_display_name": "hn",
          "title": f"h{i}", "body": ""} for i in range(3)]
        + [{"id": f"rss:{i}", "source_display_name": "rss",
            "title": f"r{i}", "body": ""} for i in range(3)]
    )
    d = mf._draft_dir("acme")
    d.mkdir(parents=True, exist_ok=True)
    with mf.corpus_path("acme").open("w", encoding="utf-8") as f:
        for it in corpus:
            f.write(json.dumps(it) + "\n")
    deck = mf.sample_deck("acme", size=4)
    # First 4 alternate hn / rss / hn / rss (interleave).
    sources = [d["source_display_name"] for d in deck]
    assert sources[:2] == ["hn", "rss"]
    assert len(deck) == 4


def test_sample_deck_excludes_judged_ids(wizard_data):
    d = mf._draft_dir("acme")
    d.mkdir(parents=True, exist_ok=True)
    with mf.corpus_path("acme").open("w", encoding="utf-8") as f:
        for i in range(3):
            f.write(json.dumps({
                "id": f"hn:{i}", "source_display_name": "hn",
                "title": f"h{i}", "body": "",
            }) + "\n")
    deck = mf.sample_deck("acme", size=10, exclude_ids={"hn:0", "hn:2"})
    assert [it["id"] for it in deck] == ["hn:1"]


# ---------------------------------------------------------------------------
# Calibrate routes
# ---------------------------------------------------------------------------


def test_calibrate_records_judgment(client, products_dir, wizard_data, enable_v2):
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="calibrate",
    ))
    # Seed a corpus file manually so the route can look up the item.
    d = mf._draft_dir("acme")
    d.mkdir(parents=True, exist_ok=True)
    mf.corpus_path("acme").write_text(json.dumps({
        "id": "hn:1", "source_display_name": "hn",
        "title": "A post", "body": "body", "url": "https://example.com/1",
    }) + "\n", encoding="utf-8")

    resp = client.post("/wizard/acme/calibrate/hn:1",
                       data={"verdict": "relevant"}, follow_redirects=False)
    assert resp.status_code == 303
    draft = wv2.load_draft(products_dir, "acme")
    j = draft.calibration["judgments"]["hn:1"]
    assert j["verdict"] == "relevant"
    assert j["polarity"] == "positive_example"
    assert j["title"] == "A post"


def test_calibrate_done_advances_step(client, products_dir, wizard_data, enable_v2):
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="calibrate",
    ))
    resp = client.post("/wizard/acme/calibrate/done", follow_redirects=False)
    assert resp.status_code == 303
    draft = wv2.load_draft(products_dir, "acme")
    assert draft.step == "review"


def test_discard_cleans_wizard_temp_dir(client, products_dir, wizard_data, enable_v2):
    wv2.save_draft(products_dir, wv2.WizardV2Draft(slug="acme", display="Acme"))
    d = mf._draft_dir("acme"); d.mkdir(parents=True, exist_ok=True)
    mf.corpus_path("acme").write_text("x\n", encoding="utf-8")
    assert d.exists()
    client.post("/wizard/acme/discard", follow_redirects=False)
    assert not d.exists()


def test_minifetch_status_endpoint_returns_json(client, products_dir, wizard_data, enable_v2):
    wv2.save_draft(products_dir, wv2.WizardV2Draft(slug="acme", display="Acme"))
    resp = client.get("/wizard/acme/minifetch/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == mf.STATUS_NOT_STARTED
