"""Step 3 (Choose sources) tests.

Contract:
- Only display sources that are READY to fetch: keyless plugins OR
  plugins whose required env vars are already set.
- LLM-suggested + ready → shown at top, pre-checked, with LLM rationale.
- Other ready plugins → shown below, unchecked, with generic rationale +
  default stream_config so the user can enable them without hunting
  down a stream editor.
- Not-ready plugins (missing required env vars) → hidden entirely.
"""

from __future__ import annotations

from types import SimpleNamespace

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


def _fake_manifest(plugin_id, display_name, required_env=None,
                    stream_fields=None):
    from sources.base import SourceManifest, FieldSpec
    connection_fields = [
        FieldSpec(name=n, label=n, type="secret", required=True)
        for n in (required_env or [])
    ]
    stream_field_specs = [
        FieldSpec(name=n, label=n, type=t, required=False)
        for n, t in (stream_fields or [])
    ]
    return SourceManifest(
        plugin_id=plugin_id, display_name=display_name,
        connection_fields=connection_fields, stream_fields=stream_field_specs,
    )


def _install_registry(monkeypatch, manifests):
    class _Plugin:
        def __init__(self, m): self.manifest = m
    class _Reg:
        def __init__(self, plugins):
            self._plugins = plugins
        def get(self, pid):
            for p in self._plugins:
                if p.manifest.plugin_id == pid:
                    return p
            return None
        def all_plugins(self):
            return list(self._plugins)
    reg = _Reg([_Plugin(m) for m in manifests])
    monkeypatch.setattr("sources.registry.get_registry", lambda: reg)


# ---------------------------------------------------------------------------
# _build_sources_view — the core filter
# ---------------------------------------------------------------------------


def test_hides_plugins_that_need_credentials_when_env_missing(monkeypatch):
    """A plugin requiring REDDIT_CLIENT_ID must NOT appear when the env
    var isn't set — the user asked to hide unconfigured sources."""
    from pipeline import wizard_v2
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("hn", "Hacker News",
                        stream_fields=[("name", "text"), ("search_queries", "textarea_list")]),
        _fake_manifest("reddit", "Reddit", required_env=["REDDIT_CLIENT_ID"]),
    ])
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    # No .env either.
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False, raising=False)

    draft = wizard_v2.WizardV2Draft(slug="acme", display="Acme")
    view = wz._build_sources_view(draft)
    plugin_ids = [v["plugin_id"] for v in view]
    assert "hn" in plugin_ids
    assert "reddit" not in plugin_ids


def test_shows_plugins_that_need_credentials_when_env_set(monkeypatch):
    from pipeline import wizard_v2
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("reddit", "Reddit",
                        required_env=["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"],
                        stream_fields=[("subreddit", "text")]),
    ])
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")

    draft = wizard_v2.WizardV2Draft(slug="acme", display="Acme")
    view = wz._build_sources_view(draft)
    plugin_ids = [v["plugin_id"] for v in view]
    assert "reddit" in plugin_ids


def test_llm_suggested_ready_sources_come_first_and_pre_checked(monkeypatch):
    from pipeline import wizard_v2
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("hn", "Hacker News",
                        stream_fields=[("name", "text"),
                                        ("search_queries", "textarea_list")]),
        _fake_manifest("rss", "RSS",
                        stream_fields=[("name", "text"), ("feed_url", "text")]),
    ])

    draft = wizard_v2.WizardV2Draft(
        slug="acme", display="Acme",
        # LLM only suggested `hn` — the view should still show rss (also
        # keyless / ready), but hn comes first + pre-checked.
        suggested_sources=[
            {"plugin_id": "hn", "enabled": True,
             "rationale": "broad tech signal",
             "stream_config": {"search_queries": ["Acme"]}},
        ],
    )
    view = wz._build_sources_view(draft)
    # Order: LLM's picks first.
    assert view[0]["plugin_id"] == "hn"
    assert view[0]["from_llm"] is True
    assert view[0]["enabled"] is True
    # RSS is the other ready plugin, listed after and NOT pre-checked.
    other = [v for v in view if v["plugin_id"] == "rss"][0]
    assert other["from_llm"] is False
    assert other["enabled"] is False


def test_llm_suggested_but_unready_are_dropped(monkeypatch):
    """LLM suggested Reddit, but no key is set. That row must NOT appear
    (previous behavior would have shown it as 'needs key'; user asked to
    hide those)."""
    from pipeline import wizard_v2
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("hn", "Hacker News",
                        stream_fields=[("search_queries", "textarea_list")]),
        _fake_manifest("reddit", "Reddit",
                        required_env=["REDDIT_CLIENT_ID"]),
    ])
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)

    draft = wizard_v2.WizardV2Draft(
        slug="acme", display="Acme",
        suggested_sources=[
            {"plugin_id": "hn", "enabled": True, "rationale": "x"},
            {"plugin_id": "reddit", "enabled": True, "rationale": "y"},
        ],
    )
    view = wz._build_sources_view(draft)
    plugin_ids = [v["plugin_id"] for v in view]
    assert plugin_ids == ["hn"]


def test_default_stream_config_for_search_based_plugins(monkeypatch):
    """A non-suggested keyless plugin gets `search_queries` seeded with
    the product display + aliases so the user can toggle it on without
    also having to hand-edit the stream."""
    from pipeline import wizard_v2
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("hn", "Hacker News",
                        stream_fields=[("name", "text"),
                                        ("search_queries", "textarea_list")]),
    ])
    draft = wizard_v2.WizardV2Draft(
        slug="acme", display="Acme Cloud",
        aliases=["Acme"], suggested_sources=[],
    )
    view = wz._build_sources_view(draft)
    hn = view[0]
    assert hn["from_llm"] is False
    assert hn["stream_config"]["search_queries"] == ["Acme Cloud", "Acme"]
    assert hn["stream_config"]["name"] == "hn-acme"


# ---------------------------------------------------------------------------
# Save handler — new (non-suggested) sources get added to the draft
# ---------------------------------------------------------------------------


def test_plugin_requiring_feed_url_is_shown_with_inline_config_metadata(monkeypatch):
    """microsoft_community and rss need a `feed_url` per stream. Rather
    than hiding them (which loses UX), the wizard now surfaces them WITH
    metadata describing which required fields the user must fill inline.
    The Step 3 template then renders an inline textarea for each."""
    from pipeline import wizard_v2
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("hn", "Hacker News",
                        stream_fields=[("name", "text"),
                                        ("search_queries", "textarea_list")]),
        _fake_manifest("rss", "RSS",
                        stream_fields=[("name", "text"),
                                        ("feed_url", "text")]),
        _fake_manifest("microsoft_community", "MS Tech Community",
                        stream_fields=[("name", "text"),
                                        ("feed_url", "text")]),
    ])
    from sources.registry import get_registry
    reg = get_registry()
    for pid in ("rss", "microsoft_community"):
        for f in reg.get(pid).manifest.stream_fields:
            if f.name == "feed_url":
                object.__setattr__(f, "required", True)

    draft = wizard_v2.WizardV2Draft(slug="acme", display="Acme",
                                    suggested_sources=[])
    view = wz._build_sources_view(draft)
    plugin_ids = [v["plugin_id"] for v in view]
    # All three now appear.
    assert set(plugin_ids) == {"hn", "rss", "microsoft_community"}
    # rss + microsoft_community carry required-field metadata.
    rss = [v for v in view if v["plugin_id"] == "rss"][0]
    assert rss["needs_inline_config"] is True
    assert [f["name"] for f in rss["required_stream_fields"]] == ["feed_url"]
    # hn doesn't (search_queries is auto-filled from display + aliases).
    hn = [v for v in view if v["plugin_id"] == "hn"][0]
    assert hn["needs_inline_config"] is False


def test_llm_provided_feed_url_pre_populates_inline_input(monkeypatch):
    """LLM's suggested value shows up as the pre-populated textarea
    content on Step 3 so the user just confirms / edits."""
    from pipeline import wizard_v2
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("microsoft_community", "MS Tech Community",
                        stream_fields=[("name", "text"),
                                        ("feed_url", "text")]),
    ])
    from sources.registry import get_registry
    for f in get_registry().get("microsoft_community").manifest.stream_fields:
        if f.name == "feed_url":
            object.__setattr__(f, "required", True)

    llm_url = "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/Category?category.id=Windows"
    draft = wizard_v2.WizardV2Draft(
        slug="acme", display="Acme",
        suggested_sources=[
            {"plugin_id": "microsoft_community", "enabled": True,
             "rationale": "windows discussion",
             "stream_config": {"feed_url": llm_url}},
        ],
    )
    view = wz._build_sources_view(draft)
    ms = [v for v in view if v["plugin_id"] == "microsoft_community"][0]
    # Pre-populated for the inline textarea render.
    assert ms["field_values"]["feed_url"] == [llm_url]


def test_save_handler_parses_multiple_subreddits_as_multiple_streams(
    monkeypatch, tmp_path,
):
    """User types 3 subreddits (one per line). Save must create 3
    streams under the reddit source so each subreddit gets fetched."""
    from starlette.datastructures import FormData
    from pipeline import wizard_v2
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("reddit", "Reddit",
                        required_env=["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"],
                        stream_fields=[("name", "text"), ("subreddit", "text")]),
    ])
    from sources.registry import get_registry
    for f in get_registry().get("reddit").manifest.stream_fields:
        if f.name == "subreddit":
            object.__setattr__(f, "required", True)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")

    # Draft with the plugin already in suggested_sources but empty
    # stream_config — user is filling it in on Step 3.
    draft = wizard_v2.WizardV2Draft(
        slug="acme", display="Acme",
        suggested_sources=[
            {"plugin_id": "reddit", "enabled": True, "stream_config": {}},
        ],
    )
    # Simulate the form submitted by Step 3.
    form = FormData([
        ("src_enabled", "reddit"),
        ("stream__reddit__subreddit", "pcaudio\nWindows11\nWindowsHelp"),
    ])
    view_by_id = {v["plugin_id"]: v for v in wz._build_sources_view(draft)}
    wz._apply_inline_stream_config(draft, form, view_by_id)

    cfg = draft.suggested_sources[0]["stream_config"]
    # First subreddit lives on the primary stream config.
    assert cfg["subreddit"] == "pcaudio"
    # Extras stashed for materialize / minifetch to expand.
    extras = cfg["_extra_streams"]
    assert [e["subreddit"] for e in extras] == ["Windows11", "WindowsHelp"]


def test_llm_stream_suggestions_are_surfaced_as_checkboxes(monkeypatch):
    """The wizard cached LLM suggestions for microsoft_community's feed_url.
    _build_sources_view must attach them to the view row under
    `field_suggestions` so the template can render checkboxes."""
    from pipeline import wizard_v2
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("microsoft_community", "MS Tech Community",
                        stream_fields=[("name", "text"),
                                        ("feed_url", "text")]),
    ])
    from sources.registry import get_registry
    for f in get_registry().get("microsoft_community").manifest.stream_fields:
        if f.name == "feed_url":
            object.__setattr__(f, "required", True)

    draft = wizard_v2.WizardV2Draft(
        slug="acme", display="Acme",
        stream_suggestions={
            "microsoft_community": {
                "feed_url": [
                    {"value": "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/Category?category.id=Windows",
                     "rationale": "Windows category"},
                    {"value": "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/Category?category.id=Microsoft365",
                     "rationale": "M365 category"},
                ],
            },
        },
    )
    view = wz._build_sources_view(draft)
    ms = [v for v in view if v["plugin_id"] == "microsoft_community"][0]
    assert len(ms["field_suggestions"]["feed_url"]) == 2
    assert ms["field_suggestions"]["feed_url"][0]["value"].endswith("category.id=Windows")


def test_save_handler_merges_checked_suggestions_with_typed_lines(monkeypatch):
    """User checks two suggested subreddits AND types one more in the
    textarea. Save must combine them into 3 streams (checked-first, then
    typed, deduped)."""
    from starlette.datastructures import FormData
    from pipeline import wizard_v2
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("reddit", "Reddit",
                        required_env=["REDDIT_CLIENT_ID"],
                        stream_fields=[("name", "text"), ("subreddit", "text")]),
    ])
    from sources.registry import get_registry
    for f in get_registry().get("reddit").manifest.stream_fields:
        if f.name == "subreddit":
            object.__setattr__(f, "required", True)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")

    draft = wizard_v2.WizardV2Draft(
        slug="acme", display="Acme",
        suggested_sources=[
            {"plugin_id": "reddit", "enabled": True, "stream_config": {}},
        ],
    )
    form = FormData([
        ("src_enabled", "reddit"),
        ("suggest__reddit__subreddit", "pcaudio"),
        ("suggest__reddit__subreddit", "Windows11"),
        ("stream__reddit__subreddit", "WindowsHelp"),
    ])
    view_by_id = {v["plugin_id"]: v for v in wz._build_sources_view(draft)}
    wz._apply_inline_stream_config(draft, form, view_by_id)

    cfg = draft.suggested_sources[0]["stream_config"]
    assert cfg["subreddit"] == "pcaudio"  # first checked = primary
    extras = [e["subreddit"] for e in cfg["_extra_streams"]]
    assert extras == ["Windows11", "WindowsHelp"]


def test_save_handler_dedupes_when_typed_line_matches_suggestion(monkeypatch):
    """If the user checked a suggestion AND typed the same value in the
    textarea, it should only generate ONE stream — no duplicate."""
    from starlette.datastructures import FormData
    from pipeline import wizard_v2
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("microsoft_community", "MS Tech Community",
                        stream_fields=[("name", "text"),
                                        ("feed_url", "text")]),
    ])
    from sources.registry import get_registry
    for f in get_registry().get("microsoft_community").manifest.stream_fields:
        if f.name == "feed_url":
            object.__setattr__(f, "required", True)

    url = "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/Category?category.id=Windows"
    draft = wizard_v2.WizardV2Draft(
        slug="acme", display="Acme",
        suggested_sources=[
            {"plugin_id": "microsoft_community", "enabled": True,
             "stream_config": {}},
        ],
    )
    form = FormData([
        ("src_enabled", "microsoft_community"),
        ("suggest__microsoft_community__feed_url", url),
        ("stream__microsoft_community__feed_url", url),  # same URL in textarea
    ])
    view_by_id = {v["plugin_id"]: v for v in wz._build_sources_view(draft)}
    wz._apply_inline_stream_config(draft, form, view_by_id)

    cfg = draft.suggested_sources[0]["stream_config"]
    assert cfg["feed_url"] == url
    # No extras — the duplicate was deduped away.
    assert cfg.get("_extra_streams") in (None, [])


def test_ensure_stream_suggestions_for_selected_calls_llm_only_once_per_field(
    monkeypatch, tmp_path,
):
    """Suggestions are cached on the draft so a subsequent page load
    doesn't fire another LLM call. Also only fires for SELECTED sources —
    unchecked sources don't burn tokens."""
    from pipeline import wizard_v2, stream_suggestions
    from webui import wizard as wz

    _install_registry(monkeypatch, [
        _fake_manifest("reddit", "Reddit",
                        required_env=["REDDIT_CLIENT_ID"],
                        stream_fields=[("name", "text"), ("subreddit", "text")]),
        _fake_manifest("microsoft_community", "MS Tech Community",
                        stream_fields=[("name", "text"),
                                        ("feed_url", "text")]),
    ])
    from sources.registry import get_registry
    for f in get_registry().get("reddit").manifest.stream_fields:
        if f.name == "subreddit":
            object.__setattr__(f, "required", True)
    for f in get_registry().get("microsoft_community").manifest.stream_fields:
        if f.name == "feed_url":
            object.__setattr__(f, "required", True)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")

    prods = tmp_path / "products"; prods.mkdir()
    monkeypatch.setattr("pipeline.product.PRODUCTS_DIR", prods)

    calls: list[str] = []
    def _fake_suggest(profile_facts, pid, field_name, *, field_help="",
                       product_id_for_budget=None):
        calls.append(f"{pid}:{field_name}")
        return [{"value": "pcaudio", "rationale": "audio-focused"}]
    monkeypatch.setattr(stream_suggestions, "suggest_stream_identifiers",
                        _fake_suggest)

    draft = wizard_v2.WizardV2Draft(
        slug="acme", display="Acme",
        suggested_sources=[
            {"plugin_id": "reddit", "enabled": True, "stream_config": {}},
            {"plugin_id": "microsoft_community", "enabled": False,
             "stream_config": {}},
        ],
    )
    wizard_v2.save_draft(prods, draft)
    view = wz._build_sources_view(draft)
    wz._ensure_stream_suggestions_for_selected(draft, view)
    # Only reddit (enabled) triggered a call; microsoft_community (unchecked)
    # did NOT — that's the whole point of running lazily on the configure phase.
    assert calls == ["reddit:subreddit"]
    # Second call — no new LLM invocation because cached.
    wz._ensure_stream_suggestions_for_selected(draft, view)
    assert calls == ["reddit:subreddit"]


def test_pick_phase_advance_moves_to_configure_sub_phase(monkeypatch, client,
                                                          products_dir, enable_v2):
    """The pick sub-phase POST with action=advance moves the draft to
    sources_substep='configure' — not to step='calibrate'. Configuration
    happens on the next screen (the actual sub-wizard flow)."""
    from pipeline import wizard_v2
    _install_registry(monkeypatch, [
        _fake_manifest("hn", "Hacker News",
                        stream_fields=[("name", "text"),
                                        ("search_queries", "textarea_list")]),
    ])
    wizard_v2.save_draft(products_dir, wizard_v2.WizardV2Draft(
        slug="acme", display="Acme", step="sources",
        sources_substep="pick",
        suggested_sources=[
            {"plugin_id": "hn", "enabled": False, "requires_key": False,
             "stream_config": {}},
        ],
    ))
    client.post("/wizard/acme/sources",
                data={"src_enabled": "hn", "action": "advance"},
                follow_redirects=False)
    draft = wizard_v2.load_draft(products_dir, "acme")
    # Step is still `sources` (we haven't left the parent step)
    assert draft.step == "sources"
    # But sub-phase advanced.
    assert draft.sources_substep == "configure"
    # And the toggle we picked was persisted.
    assert draft.suggested_sources[0]["enabled"] is True


def test_configure_phase_advance_starts_minifetch_and_moves_to_calibrate(
    monkeypatch, client, products_dir, enable_v2,
):
    """The configure sub-phase POST with action=advance finally kicks off
    the mini-fetch and moves to calibrate — same behavior we used to
    have on Step 3 pre-split, just gated behind the sub-wizard."""
    from pipeline import wizard_v2
    _install_registry(monkeypatch, [
        _fake_manifest("hn", "Hacker News",
                        stream_fields=[("name", "text"),
                                        ("search_queries", "textarea_list")]),
    ])
    started = []
    monkeypatch.setattr("pipeline.minifetch.start_minifetch",
                        lambda slug, srcs, **kw: started.append((slug, srcs)))
    wizard_v2.save_draft(products_dir, wizard_v2.WizardV2Draft(
        slug="acme", display="Acme", step="sources",
        sources_substep="configure",
        suggested_sources=[
            {"plugin_id": "hn", "enabled": True, "requires_key": False,
             "stream_config": {"search_queries": ["Acme"]}, "rationale": "hn"},
        ],
    ))
    client.post("/wizard/acme/sources",
                data={"action": "advance"},
                follow_redirects=False)
    draft = wizard_v2.load_draft(products_dir, "acme")
    assert draft.step == "calibrate"
    assert started, "start_minifetch should have been invoked"
    # After advancing to calibrate, sub-phase resets to pick so back-nav
    # returns the user to the picker.
    assert draft.sources_substep == "pick"


def test_configure_phase_back_button_returns_to_pick(monkeypatch, client,
                                                      products_dir, enable_v2):
    """action=back from the configure phase returns to pick — does NOT
    step out of Step 3 entirely."""
    from pipeline import wizard_v2
    _install_registry(monkeypatch, [
        _fake_manifest("hn", "Hacker News",
                        stream_fields=[("search_queries", "textarea_list"),
                                        ("name", "text")]),
    ])
    wizard_v2.save_draft(products_dir, wizard_v2.WizardV2Draft(
        slug="acme", display="Acme", step="sources",
        sources_substep="configure",
        suggested_sources=[
            {"plugin_id": "hn", "enabled": True, "stream_config": {}},
        ],
    ))
    client.post("/wizard/acme/sources",
                data={"action": "back"},
                follow_redirects=False)
    draft = wizard_v2.load_draft(products_dir, "acme")
    assert draft.step == "sources"
    assert draft.sources_substep == "pick"


def test_pick_phase_does_not_trigger_llm_suggestions(monkeypatch, client,
                                                      products_dir, enable_v2):
    """The pick phase must NOT call the LLM — that would burn tokens for
    sources the user hasn't opted into. Suggestions only fire on the
    configure phase for the picked sources."""
    from pipeline import wizard_v2, stream_suggestions
    _install_registry(monkeypatch, [
        _fake_manifest("reddit", "Reddit",
                        required_env=["REDDIT_CLIENT_ID"],
                        stream_fields=[("name", "text"), ("subreddit", "text")]),
    ])
    from sources.registry import get_registry
    for f in get_registry().get("reddit").manifest.stream_fields:
        if f.name == "subreddit":
            object.__setattr__(f, "required", True)
    monkeypatch.setenv("REDDIT_CLIENT_ID", "id")
    monkeypatch.setattr(stream_suggestions, "suggest_stream_identifiers",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("must not call LLM on pick phase")))
    wizard_v2.save_draft(products_dir, wizard_v2.WizardV2Draft(
        slug="acme", display="Acme", step="sources",
        sources_substep="pick",
        suggested_sources=[
            {"plugin_id": "reddit", "enabled": False, "stream_config": {}},
        ],
    ))
    resp = client.get("/wizard/acme")
    assert resp.status_code == 200
    # Pick phase renders; no LLM call. (The raise above would have surfaced.)


def test_initial_draft_sources_start_unchecked(monkeypatch):
    """After the LLM drafts a profile in `apply_profile_draft`, the
    suggested sources must land unchecked — Step 3's pick screen is
    where the user opts in explicitly."""
    from pipeline import wizard_v2, profile_draft
    prof = profile_draft.ProfileDraft(
        description="d",
        suggested_sources=[
            profile_draft.SuggestedSource(plugin_id="hn", requires_key=False),
            profile_draft.SuggestedSource(plugin_id="reddit", requires_key=False),
        ],
    )
    draft = wizard_v2.WizardV2Draft(slug="acme", display="Acme")
    wizard_v2.apply_profile_draft(draft, prof)
    assert all(not s["enabled"] for s in draft.suggested_sources)


def test_materialize_expands_extra_streams_into_yaml_streams(monkeypatch, tmp_path):
    """The materializer must emit multiple entries under `streams:` when
    the wizard collected multiple identifiers (subreddits, feed_urls,
    etc.). Otherwise all but the first identifier gets silently dropped."""
    from pipeline import wizard_v2
    entry = wizard_v2._source_entry({
        "plugin_id": "reddit",
        "stream_config": {
            "subreddit": "pcaudio",
            "name": "reddit-acme",
            "_extra_streams": [
                {"subreddit": "Windows11"},
                {"subreddit": "WindowsHelp"},
            ],
        },
    }, slug="acme")
    subreddits = [s["subreddit"] for s in entry["streams"]]
    assert subreddits == ["pcaudio", "Windows11", "WindowsHelp"]
    # Every stream has a name so the pipeline's cursor logic keeps them
    # distinct.
    names = [s["name"] for s in entry["streams"]]
    assert len(set(names)) == len(names)


def test_enabling_non_suggested_source_adds_it_to_draft(monkeypatch,
                                                         client, products_dir,
                                                         enable_v2):
    """User checks 'rss' (which the LLM didn't suggest). The save handler
    must materialize it into draft.suggested_sources so downstream code
    (minifetch / materialize) sees it."""
    from pipeline import wizard_v2
    _install_registry(monkeypatch, [
        _fake_manifest("hn", "Hacker News",
                        stream_fields=[("search_queries", "textarea_list"), ("name", "text")]),
        _fake_manifest("rss", "RSS",
                        stream_fields=[("feed_url", "text"), ("name", "text")]),
    ])
    wizard_v2.save_draft(products_dir, wizard_v2.WizardV2Draft(
        slug="acme", display="Acme", step="sources",
        suggested_sources=[
            {"plugin_id": "hn", "enabled": True, "requires_key": False,
             "stream_config": {"search_queries": ["Acme"]}, "rationale": "hn"},
        ],
    ))
    client.post("/wizard/acme/sources",
                data={"src_enabled": ["hn", "rss"], "action": "save"},
                follow_redirects=False)
    draft = wizard_v2.load_draft(products_dir, "acme")
    plugin_ids = [s["plugin_id"] for s in draft.suggested_sources]
    # RSS is now in the draft's suggested_sources list.
    assert set(plugin_ids) == {"hn", "rss"}
    rss = [s for s in draft.suggested_sources if s["plugin_id"] == "rss"][0]
    assert rss["enabled"] is True
