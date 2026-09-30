"""Phase 3 tests — wizard v2 routes + draft store.

Covers:
- /wizard 403 when flag off
- POST /wizard/draft creates a draft file, runs ProfileDraft, redirects to profile
- GET /wizard/{slug} renders the profile step with drafted content
- POST /wizard/{slug}/profile round-trips chip edits
- Regen cap enforced (redirects with error after MAX_REGENERATIONS_PER_SECTION)
- Assistant-LLM absent → draft still created with a "drafting incomplete" banner
- Discard removes the file
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pipeline import profile_draft as pd
from pipeline import wizard_v2 as wv2
from pipeline.profile_draft import DraftResult, ProfileDraft, SuggestedSource


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
    # Bust the load_product LRU cache
    from pipeline import product as _p, features as _f
    _p.clear_cache(); _f.clear_cache()
    return d


@pytest.fixture
def enable_v2(monkeypatch):
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: flag == "wizard_v2_enabled")


@pytest.fixture
def disable_v2(monkeypatch):
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: False)


@pytest.fixture
def fake_draft_service(monkeypatch):
    """Stub `draft_profile` to return a deterministic DraftResult."""
    calls: list = []

    def _draft(name, url_or_description, goals=None, *, product_id_for_budget=None):
        calls.append({
            "name": name, "url_or_description": url_or_description,
            "goals": goals or [], "product_id_for_budget": product_id_for_budget,
        })
        return DraftResult(
            profile=ProfileDraft(
                description="Drafted description.",
                aliases=["A1", "A2"],
                not_to_be_confused_with=["Nope"],
                competitors=["Rival"],
                scope_in=["cloud bugs"],
                scope_out=["marketing"],
                suggested_sources=[
                    SuggestedSource(plugin_id="hn", stream_config={"search_queries": ["Acme"]},
                                    rationale="broad tech signal", requires_key=False),
                ],
            ),
            page_fetch_failed=False, fetched_chars=0,
        )
    monkeypatch.setattr(pd, "draft_profile", _draft)
    # Also patch the router's imported reference (routes bind at import time
    # so the alias points at the original before monkeypatch).
    monkeypatch.setattr("webui.wizard._profile_draft.draft_profile", _draft)
    return calls


@pytest.fixture
def fake_no_llm(monkeypatch):
    """Stub `draft_profile` to look like assistant LLM unconfigured."""
    def _draft(name, url_or_description, goals=None, *, product_id_for_budget=None):
        return DraftResult(profile=None, error_message="assistant LLM not configured")
    monkeypatch.setattr(pd, "draft_profile", _draft)
    monkeypatch.setattr("webui.wizard._profile_draft.draft_profile", _draft)


@pytest.fixture
def client():
    from webui.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Flag gating
# ---------------------------------------------------------------------------


def test_landing_returns_403_when_flag_off(client, products_dir, disable_v2):
    resp = client.get("/wizard")
    assert resp.status_code == 403


def test_draft_route_returns_403_when_flag_off(client, products_dir, disable_v2):
    resp = client.post("/wizard/draft", data={"display": "Acme"})
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Happy path — Screen 1 → Screen 2
# ---------------------------------------------------------------------------


def test_bare_domain_gets_normalized_to_url(client, products_dir, enable_v2,
                                             fake_draft_service):
    """`vapi.ai` (no protocol) is the common user pattern. Draft.url must be
    populated so the confirm-profile page shows it in the URL field rather
    than leaving it empty."""
    resp = client.post("/wizard/draft", data={
        "display": "VAPI",
        "url_or_description": "vapi.ai",
        "goals": ["bugs"],
    }, follow_redirects=False)
    assert resp.status_code == 303
    draft = wv2.load_draft(products_dir, "vapi")
    assert draft is not None
    assert draft.url == "https://vapi.ai"


def test_freeform_description_does_not_pollute_url_field(client, products_dir,
                                                          enable_v2, fake_draft_service):
    """Prose input shouldn't get shoved into the URL field."""
    client.post("/wizard/draft", data={
        "display": "Acme",
        "url_or_description": "A cloud storage product for teams",
    })
    draft = wv2.load_draft(products_dir, "acme")
    assert draft is not None
    assert draft.url == ""


def test_normalize_user_url_helper():
    from webui.wizard import _normalize_user_url
    assert _normalize_user_url("https://vapi.ai") == "https://vapi.ai"
    assert _normalize_user_url("http://x.example.com/y") == "http://x.example.com/y"
    assert _normalize_user_url("vapi.ai") == "https://vapi.ai"
    assert _normalize_user_url("  vapi.ai  ") == "https://vapi.ai"
    assert _normalize_user_url("notion.so") == "https://notion.so"
    assert _normalize_user_url("A cloud storage product") == ""
    assert _normalize_user_url("") == ""
    assert _normalize_user_url("   ") == ""


def test_draft_creates_file_and_redirects(client, products_dir, enable_v2, fake_draft_service):
    resp = client.post(
        "/wizard/draft",
        data={"display": "Acme", "url_or_description": "https://acme.example",
              "goals": ["bugs", "sentiment"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/wizard/acme"
    assert (products_dir / ".wizard_drafts/v2/acme.yaml").exists()
    draft = wv2.load_draft(products_dir, "acme")
    assert draft is not None
    assert draft.display == "Acme"
    assert draft.step == "profile"
    assert draft.aliases == ["A1", "A2"]
    assert draft.url == "https://acme.example"


def test_landing_lists_existing_drafts(client, products_dir, enable_v2):
    wv2.save_draft(products_dir, wv2.WizardV2Draft(slug="acme", display="Acme", step="profile"))
    resp = client.get("/wizard")
    assert resp.status_code == 200
    assert "Acme" in resp.text
    assert "step: profile" in resp.text


def test_profile_step_renders_chips(client, products_dir, enable_v2):
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="profile",
        description="d", aliases=["A1", "A2"], scope_in=["s1"],
    ))
    resp = client.get("/wizard/acme")
    assert resp.status_code == 200
    # Chip rendering
    assert "A1" in resp.text and "A2" in resp.text
    # Chip textarea shows one item per line
    assert "A1\nA2" in resp.text
    # Stepper marks profile as current
    assert "wv2-step current" in resp.text


def test_profile_save_round_trips_chips(client, products_dir, enable_v2):
    wv2.save_draft(products_dir, wv2.WizardV2Draft(slug="acme", display="Acme", step="profile"))
    resp = client.post(
        "/wizard/acme/profile",
        data={
            "description": "New desc",
            "aliases": "One\nTwo\nThree",
            "not_to_be_confused_with": "",
            "competitors": "Rival",
            "scope_in": "bug\nsync",
            "scope_out": "marketing",
            "action": "save",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    draft = wv2.load_draft(products_dir, "acme")
    assert draft.aliases == ["One", "Two", "Three"]
    assert draft.description == "New desc"
    assert draft.scope_in == ["bug", "sync"]
    assert draft.competitors == ["Rival"]


def test_profile_save_with_advance_moves_step(client, products_dir, enable_v2):
    """Profile step now advances to the new dedicated Sources step (not
    straight to Calibrate — that split landed alongside step 3)."""
    wv2.save_draft(products_dir, wv2.WizardV2Draft(slug="acme", display="Acme", step="profile"))
    client.post(
        "/wizard/acme/profile",
        data={"description": "d", "action": "advance"},
        follow_redirects=False,
    )
    draft = wv2.load_draft(products_dir, "acme")
    assert draft.step == "sources"


def test_sources_step_advance_starts_minifetch_and_moves_to_calibrate(
    client, products_dir, enable_v2, monkeypatch,
):
    """Sources that need no per-stream config (HN search_queries auto-fill)
    skip configure: pick Continue starts minifetch and moves to calibrate."""
    started = []
    monkeypatch.setattr("pipeline.minifetch.start_minifetch",
                        lambda slug, srcs, **kw: started.append((slug, srcs)))
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="sources",
        sources_substep="pick",
        suggested_sources=[
            {"plugin_id": "hn", "enabled": False, "requires_key": False,
             "stream_config": {"search_queries": ["Acme"]}},
        ],
    ))
    client.post("/wizard/acme/sources",
                data={"src_enabled": "hn", "action": "advance"},
                follow_redirects=False)
    draft = wv2.load_draft(products_dir, "acme")
    assert draft.step == "calibrate"
    assert draft.suggested_sources[0]["enabled"] is True
    assert started, "start_minifetch should run when configure is skipped"


def test_sources_step_save_stays_on_sources(client, products_dir, enable_v2, monkeypatch):
    """`action=save` persists toggles without kicking off a fetch."""
    monkeypatch.setattr("pipeline.minifetch.start_minifetch",
                        lambda slug, srcs, **kw: (_ for _ in ()).throw(
                            AssertionError("should not have started fetch")))
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="sources",
        suggested_sources=[
            {"plugin_id": "hn", "enabled": False, "requires_key": False},
        ],
    ))
    client.post("/wizard/acme/sources",
                data={"src_enabled": "hn", "action": "save"},
                follow_redirects=False)
    draft = wv2.load_draft(products_dir, "acme")
    assert draft.step == "sources"
    assert draft.suggested_sources[0]["enabled"] is True


def test_back_button_moves_to_previous_step(client, products_dir, enable_v2):
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="calibrate",
    ))
    resp = client.post("/wizard/acme/back", follow_redirects=False)
    assert resp.status_code == 303
    draft = wv2.load_draft(products_dir, "acme")
    assert draft.step == "sources"


def test_back_from_describe_is_a_noop(client, products_dir, enable_v2):
    """First step has no 'previous' — the route stays put rather than
    coughing up an error."""
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="describe",
    ))
    client.post("/wizard/acme/back", follow_redirects=False)
    draft = wv2.load_draft(products_dir, "acme")
    assert draft.step == "describe"


def test_discard_with_return_url_stays_on_home(client, products_dir, enable_v2):
    """Discard from the home page must NOT redirect to /wizard (which
    triggers the first-run wizard). It should return the user to /."""
    wv2.save_draft(products_dir, wv2.WizardV2Draft(slug="acme", display="Acme"))
    resp = client.post("/wizard/acme/discard",
                       data={"return_url": "/"},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_discard_default_still_returns_to_wizard(client, products_dir, enable_v2):
    """Without an explicit return_url, discard from inside the wizard
    keeps its historic behavior — go to /wizard landing."""
    wv2.save_draft(products_dir, wv2.WizardV2Draft(slug="acme", display="Acme"))
    resp = client.post("/wizard/acme/discard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/wizard"


# ---------------------------------------------------------------------------
# Regeneration cap
# ---------------------------------------------------------------------------


def test_regen_cap_blocks_after_three_regens(client, products_dir, enable_v2, fake_draft_service):
    draft = wv2.WizardV2Draft(slug="acme", display="Acme", step="profile")
    draft.regenerations["aliases"] = wv2.MAX_REGENERATIONS_PER_SECTION
    wv2.save_draft(products_dir, draft)
    resp = client.post("/wizard/acme/regen/aliases", follow_redirects=False)
    assert resp.status_code == 303
    assert "error=regen+cap+reached" in resp.headers["location"]


def test_regen_below_cap_updates_only_that_section(client, products_dir,
                                                   enable_v2, fake_draft_service):
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="profile",
        aliases=["existing"], scope_in=["existing scope"],
    ))
    resp = client.post("/wizard/acme/regen/aliases", follow_redirects=False)
    assert resp.status_code == 303
    draft = wv2.load_draft(products_dir, "acme")
    assert draft.aliases == ["A1", "A2"]  # replaced from stub
    assert draft.scope_in == ["existing scope"]  # untouched
    assert draft.regenerations.get("aliases") == 1


def test_regen_preserves_form_edits_on_other_sections(client, products_dir,
                                                      enable_v2, fake_draft_service):
    """Regression: the regen buttons live inside the profile edit form via
    <button formaction=...>. That means the parent form's data submits with
    the regen POST — user edits on other sections must be saved, not lost."""
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="profile",
        description="original desc", competitors=["OldRival"],
        scope_in=["old scope"],
    ))
    # User has edited description + scope_in + competitors, then clicked
    # Regenerate on aliases. All these fields are in the form data.
    resp = client.post("/wizard/acme/regen/aliases", data={
        "description": "user's edited description",
        "url": "https://edited.example",
        "aliases": "should-be-ignored\nbecause-target-section",
        "not_to_be_confused_with": "Edited Confuser",
        "competitors": "New Rival A\nNew Rival B",
        "scope_in": "edited scope",
        "scope_out": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    draft = wv2.load_draft(products_dir, "acme")
    # Non-target sections took the user's edits.
    assert draft.description == "user's edited description"
    assert draft.url == "https://edited.example"
    assert draft.competitors == ["New Rival A", "New Rival B"]
    assert draft.scope_in == ["edited scope"]
    assert draft.not_to_be_confused_with == ["Edited Confuser"]
    assert draft.scope_out == []
    # Target section got the LLM's fresh output, NOT the user's edit.
    assert draft.aliases == ["A1", "A2"]


def test_regen_redirect_includes_scroll_fragment(client, products_dir,
                                                   enable_v2, fake_draft_service):
    """After regen the redirect Location must carry `#section-<name>` so
    the browser scrolls back to the regenerated section instead of
    landing at the top of the page."""
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="profile", aliases=["existing"],
    ))
    resp = client.post("/wizard/acme/regen/aliases", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].endswith("#section-aliases")


def test_regen_saves_form_edits_even_when_llm_fails(client, products_dir,
                                                    enable_v2, monkeypatch):
    """If the drafting LLM call fails, we still persist the user's other
    section edits — otherwise clicking Regenerate on a broken LLM would
    silently roll back everything the user typed."""
    from pipeline import profile_draft as pd
    monkeypatch.setattr(pd, "draft_profile", lambda *a, **kw: pd.DraftResult(
        profile=None, error_message="LLM+down",
    ))
    monkeypatch.setattr("webui.wizard._profile_draft.draft_profile",
                        lambda *a, **kw: pd.DraftResult(
                            profile=None, error_message="LLM+down",
                        ))
    wv2.save_draft(products_dir, wv2.WizardV2Draft(
        slug="acme", display="Acme", step="profile",
        description="original", competitors=["OldRival"],
    ))
    resp = client.post("/wizard/acme/regen/aliases", data={
        "description": "edited-desc",
        "competitors": "New Rival",
        "aliases": "", "not_to_be_confused_with": "",
        "scope_in": "", "scope_out": "",
    }, follow_redirects=False)
    assert resp.status_code == 303
    # Redirect surfaces the LLM error.
    assert "error=LLM" in resp.headers["location"]
    # But user's edits still landed.
    draft = wv2.load_draft(products_dir, "acme")
    assert draft.description == "edited-desc"
    assert draft.competitors == ["New Rival"]


def test_regen_unknown_section_returns_400(client, products_dir, enable_v2, fake_draft_service):
    wv2.save_draft(products_dir, wv2.WizardV2Draft(slug="acme", display="Acme", step="profile"))
    resp = client.post("/wizard/acme/regen/nonsense", follow_redirects=False)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Degraded path — assistant LLM unavailable
# ---------------------------------------------------------------------------


def test_draft_still_created_when_assistant_llm_missing(client, products_dir,
                                                        enable_v2, fake_no_llm):
    resp = client.post(
        "/wizard/draft", data={"display": "Acme"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    draft = wv2.load_draft(products_dir, "acme")
    assert draft is not None
    assert "assistant LLM not configured" in draft.drafting_error
    # Profile page still loads and surfaces the banner.
    r2 = client.get("/wizard/acme")
    assert r2.status_code == 200
    assert "assistant LLM not configured" in r2.text


# ---------------------------------------------------------------------------
# Discard
# ---------------------------------------------------------------------------


def test_discard_deletes_the_file(client, products_dir, enable_v2):
    wv2.save_draft(products_dir, wv2.WizardV2Draft(slug="acme", display="Acme"))
    assert (products_dir / ".wizard_drafts/v2/acme.yaml").exists()
    resp = client.post("/wizard/acme/discard", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/wizard"
    assert not (products_dir / ".wizard_drafts/v2/acme.yaml").exists()


# ---------------------------------------------------------------------------
# Slug safety
# ---------------------------------------------------------------------------


def test_draft_slug_collision_with_product_redirects_with_error(client, products_dir,
                                                                 enable_v2, fake_draft_service):
    # Simulate an existing product directory.
    (products_dir / "acme").mkdir()
    resp = client.post("/wizard/draft", data={"display": "Acme"}, follow_redirects=False)
    assert resp.status_code == 303
    assert "already+used+by+a+product" in resp.headers["location"]


def test_load_draft_rejects_path_traversal(products_dir):
    # Sanity check on the sanitizer; ../ must not resolve outside drafts_dir.
    assert wv2.load_draft(products_dir, "../etc/passwd") is None
