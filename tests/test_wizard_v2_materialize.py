"""Phase 5 tests — taxonomy proposal + materialize + review routes.

Covers:
- Taxonomy proposal falls back to starter guess when no corpus (grounded=False)
- Taxonomy proposal validates + trims LLM output (drops invalid example ids)
- Materialize produces a load_product()-valid product tree
- Materialize seeds vendors from competitors
- Materialize writes examples/{positive,negative}/*.yaml from calibration
- Materialize deletes the draft file on success
- Materialize rolls back the product dir on validation failure
- /wizard/{slug}/create redirects to /products/{slug}/runs
- /wizard/{slug}/llm persists the chooser selection
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from pipeline import product as product_mod
from pipeline import taxonomy_proposal as tp
from pipeline import wizard_v2 as wv2


# ---------------------------------------------------------------------------
# Fixtures
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
    def _fake_app_config():
        return {"paths": {"data_root": str(tmp_path / "data"),
                          "run_logs_root": str(tmp_path / "logs")}}
    monkeypatch.setattr("pipeline.config.app_config", _fake_app_config)
    monkeypatch.setattr("webui.app.app_config", _fake_app_config)
    return tmp_path


@pytest.fixture
def enable_v2(monkeypatch):
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: flag == "wizard_v2_enabled")


@pytest.fixture
def client():
    from webui.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Taxonomy proposal
# ---------------------------------------------------------------------------


def test_propose_taxonomy_empty_corpus_returns_starter_guess():
    proposal, grounded = tp.propose_taxonomy(
        {"display": "Acme", "scope_in": ["cloud sync"], "aliases": []},
        corpus=[],
    )
    assert grounded is False
    assert proposal is not None
    ids = [a.id for a in proposal.areas]
    assert "general" in ids and "bugs" in ids and "requests" in ids


def test_propose_taxonomy_missing_llm_returns_starter_guess(monkeypatch):
    monkeypatch.setattr(tp, "_contract", lambda: None)
    proposal, grounded = tp.propose_taxonomy(
        {"display": "Acme"}, corpus=[{"id": "hn:1", "title": "x", "body": ""}],
    )
    assert grounded is False
    assert proposal is not None


def test_propose_taxonomy_from_llm_trims_and_validates(monkeypatch):
    class _FakeContract:
        def call(self, spec):
            return tp.TaxonomyProposal(areas=[
                tp.ProposedArea(id="audio", display="Audio",
                                keywords=["sound", "sound", "audio"],  # dup
                                example_item_ids=["hn:1", "made-up-id"]),
                tp.ProposedArea(id="video", display="Video",
                                example_item_ids=[]),
                tp.ProposedArea(id="audio", display="dup id"),  # dup — dropped
                tp.ProposedArea(id="", display="empty id"),     # dropped
            ])
    monkeypatch.setattr(tp, "_contract", lambda: _FakeContract())

    proposal, grounded = tp.propose_taxonomy(
        {"display": "Acme"},
        corpus=[{"id": "hn:1", "title": "t", "body": "b"}],
    )
    assert grounded is True
    ids = [a.id for a in proposal.areas]
    assert ids == ["audio", "video"]
    # made-up-id dropped since not in corpus_ids.
    assert proposal.areas[0].example_item_ids == ["hn:1"]
    # keywords deduped
    assert proposal.areas[0].keywords == ["sound", "audio"]


def test_to_taxonomy_yaml_has_at_least_one_feature_per_area():
    proposal = tp.TaxonomyProposal(areas=[
        tp.ProposedArea(id="a", display="A", description="desc-a"),
    ])
    blob = tp.to_taxonomy_yaml(proposal, version="2026-07-25")
    assert blob["areas"][0]["features"], "area must have >=1 feature or load_product fails"
    assert blob["areas"][0]["features"][0]["id"] == "a"


# ---------------------------------------------------------------------------
# Materialize
# ---------------------------------------------------------------------------


def _make_draft(slug="acme") -> wv2.WizardV2Draft:
    return wv2.WizardV2Draft(
        slug=slug, display="Acme", step="review",
        description="A cloud storage product.",
        aliases=["Acme Cloud"],
        competitors=["Rival"],
        scope_in=["sync bugs"],
        scope_out=["marketing"],
        suggested_sources=[
            {"plugin_id": "hn", "enabled": True, "requires_key": False,
             "stream_config": {"search_queries": ["Acme"]}},
        ],
        proposed_taxonomy={
            "version": "2026-07-25",
            "areas": [
                {"id": "general", "display": "General", "enabled": True,
                 "keywords": [], "entity_type_hint": [],
                 "features": [{"id": "general", "display": "General",
                               "description": "General discussion."}]},
            ],
        },
        calibration={"judgments": {
            "hn:1": {"verdict": "relevant", "polarity": "positive_example",
                     "item_id": "hn:1", "title": "A real post",
                     "body": "the body", "source_url": "https://ex/1",
                     "judged_at": "2026-07-25T12:00:00+00:00"},
            "hn:2": {"verdict": "not_relevant", "polarity": "negative_example",
                     "item_id": "hn:2", "title": "off-topic",
                     "body": "unrelated", "source_url": "",
                     "judged_at": "2026-07-25T12:01:00+00:00"},
        }},
    )


def test_materialize_produces_loadable_product(products_dir):
    draft = _make_draft()
    wv2.save_draft(products_dir, draft)
    target = wv2.materialize(draft, products_dir)
    assert target.is_dir()
    spec = product_mod.load_product("acme")
    assert spec.display == "Acme"
    assert spec.aliases == ["Acme Cloud"]
    assert spec.competitors == ["Rival"]
    # Taxonomy is our proposal, not the scaffold default.
    assert [a["id"] for a in spec.taxonomy["areas"]] == ["general"]
    # Snippets from calibration are on disk.
    pos = list((target / "examples" / "positive").glob("*.yaml"))
    neg = list((target / "examples" / "negative").glob("*.yaml"))
    assert len(pos) == 1 and len(neg) == 1
    # Draft file has been removed.
    assert wv2.load_draft(products_dir, "acme") is None


def test_materialize_seeds_vendors_from_competitors(products_dir):
    draft = _make_draft()
    wv2.save_draft(products_dir, draft)
    target = wv2.materialize(draft, products_dir)
    vendors = yaml.safe_load((target / "vendors.yaml").read_text(encoding="utf-8"))
    names = [v["name"] for v in vendors["vendors"]]
    assert names == ["Rival"]


def test_materialize_skips_disabled_sources(products_dir):
    draft = _make_draft()
    draft.suggested_sources.append({
        "plugin_id": "reddit", "enabled": False, "requires_key": True, "stream_config": {},
    })
    wv2.save_draft(products_dir, draft)
    target = wv2.materialize(draft, products_dir)
    sources = yaml.safe_load((target / "sources.yaml").read_text(encoding="utf-8"))
    assert [s["type"] for s in sources["sources"]] == ["hn"]


def test_materialize_rolls_back_on_validation_failure(products_dir):
    draft = _make_draft()
    # Give the proposed taxonomy an area with no features → load_product will
    # raise ValueError, forcing rollback.
    draft.proposed_taxonomy = {
        "version": "2026-07-25",
        "areas": [{"id": "broken", "display": "Broken", "enabled": True,
                   "keywords": [], "entity_type_hint": [], "features": []}],
    }
    wv2.save_draft(products_dir, draft)
    with pytest.raises(ValueError):
        wv2.materialize(draft, products_dir)
    # Product dir removed by rollback.
    assert not (products_dir / "acme").exists()
    # Draft file NOT removed — user can retry after fixing.
    assert wv2.load_draft(products_dir, "acme") is not None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _stub_llm_options(monkeypatch):
    """Force _available_llm_options to return one 'skip' entry so tests
    can supply option_index=0 without touching real .env state."""
    monkeypatch.setattr("webui.wizard._available_llm_options", lambda: [
        {"choice": "skip", "provider": "", "display": "Skip",
         "model": "", "endpoint": "", "source": "none", "badge": ""},
    ])


def test_create_route_materializes_and_does_not_auto_run(
    client, products_dir, wizard_data, enable_v2, monkeypatch,
):
    """Create just materializes the product now — users trigger runs
    manually on the Runs page. Historically we auto-ran here, which
    surprised users who expected 'Create' to mean 'create'."""
    draft = _make_draft()
    wv2.save_draft(products_dir, draft)
    _stub_llm_options(monkeypatch)
    # Fail loudly if the route ever calls a subprocess-spawning helper.
    run_calls = []
    monkeypatch.setattr("subprocess.Popen",
                        lambda *a, **kw: run_calls.append(a) or object())

    resp = client.post("/wizard/acme/create",
                       data={"option_index": "0"},
                       follow_redirects=False)
    assert resp.status_code == 303
    # Redirect lands on Runs page with a friendly notice.
    assert resp.headers["location"].startswith("/products/acme/runs")
    assert "notice=" in resp.headers["location"]
    # Product exists on disk.
    assert (products_dir / "acme").is_dir()
    # And we did NOT auto-launch the pipeline.
    assert run_calls == [], "Create must not spawn a run — user triggers manually"


def test_create_route_blocks_when_no_llm_picked(client, products_dir,
                                                 wizard_data, enable_v2, monkeypatch):
    """Submitting Create without picking an LLM must NOT materialize —
    otherwise the product ships with the scaffold's Foundry-Local
    placeholder routing (broken for everyone who doesn't have FL running)."""
    draft = _make_draft()
    wv2.save_draft(products_dir, draft)
    _stub_llm_options(monkeypatch)
    resp = client.post("/wizard/acme/create", follow_redirects=False)
    assert resp.status_code == 303
    assert "pick+an+LLM+option" in resp.headers["location"]
    # Product must not be created.
    assert not (products_dir / "acme").exists()


def test_create_route_reports_error_on_materialize_failure(client, products_dir,
                                                            wizard_data, enable_v2,
                                                            monkeypatch):
    draft = _make_draft()
    draft.proposed_taxonomy = {"version": "x", "areas": [
        {"id": "b", "display": "B", "enabled": True, "features": [],
         "keywords": [], "entity_type_hint": []},
    ]}
    wv2.save_draft(products_dir, draft)
    _stub_llm_options(monkeypatch)
    resp = client.post("/wizard/acme/create",
                       data={"option_index": "0"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert "create+failed" in resp.headers["location"]
    assert not (products_dir / "acme").exists()


def test_llm_route_persists_choice(client, products_dir, wizard_data, enable_v2, monkeypatch):
    draft = _make_draft()
    wv2.save_draft(products_dir, draft)
    # Skip the network probe.
    monkeypatch.setattr("webui.wizard._probe_llm", lambda d: (True, "ok"))
    # Stub the available-options list so the test doesn't depend on real
    # .env state (which would vary per developer). Return one hosted
    # Anthropic option at index 0.
    monkeypatch.setattr("webui.wizard._available_llm_options", lambda: [
        {"choice": "hosted", "provider": "anthropic",
         "display": "Anthropic (Claude)", "model": "claude-sonnet-4-6",
         "endpoint": "https://api.anthropic.com/v1/",
         "source": ".env", "badge": "key in .env"},
        {"choice": "skip", "provider": "", "display": "Skip",
         "model": "", "endpoint": "", "source": "none", "badge": ""},
    ])
    resp = client.post("/wizard/acme/llm", data={
        "option_index": "0",
    }, follow_redirects=False)
    assert resp.status_code == 303
    reloaded = wv2.load_draft(products_dir, "acme")
    assert reloaded.llm_choice == "hosted"
    assert reloaded.llm_provider == "anthropic"
    assert reloaded.llm_endpoint == "https://api.anthropic.com/v1/"
    assert reloaded.llm_model == "claude-sonnet-4-6"
    assert reloaded.llm_health_ok is True


def test_llm_route_skip_option_persists_and_clears_health(
    client, products_dir, wizard_data, enable_v2, monkeypatch,
):
    draft = _make_draft()
    wv2.save_draft(products_dir, draft)
    monkeypatch.setattr("webui.wizard._available_llm_options", lambda: [
        {"choice": "skip", "provider": "", "display": "Skip",
         "model": "", "endpoint": "", "source": "none", "badge": ""},
    ])
    resp = client.post("/wizard/acme/llm", data={
        "option_index": "0",
    }, follow_redirects=False)
    assert resp.status_code == 303
    reloaded = wv2.load_draft(products_dir, "acme")
    assert reloaded.llm_choice == "skip"
    assert reloaded.llm_health_ok is None


def test_llm_route_rejects_missing_option_index(
    client, products_dir, wizard_data, enable_v2,
):
    draft = _make_draft()
    wv2.save_draft(products_dir, draft)
    resp = client.post("/wizard/acme/llm", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert "error=pick+an+llm+option" in resp.headers["location"]


def test_calibrate_done_generates_taxonomy_and_advances_step(
    client, products_dir, wizard_data, enable_v2, monkeypatch,
):
    # No corpus → starter guess, no LLM call needed.
    monkeypatch.setattr("pipeline.taxonomy_proposal._contract", lambda: None)
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="calibrate",
        aliases=["A"], scope_in=["s"],
    ))
    resp = client.post("/wizard/acme/calibrate/done", follow_redirects=False)
    assert resp.status_code == 303
    draft = wv2.load_draft(products_dir, "acme")
    assert draft.step == "review"
    assert draft.proposed_taxonomy["_grounded"] is False
    assert draft.proposed_taxonomy["areas"], "starter guess populated"
