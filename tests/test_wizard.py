"""Tests for pipeline/wizard.py (POST_V1_PLAN §4.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pipeline.wizard import (
    LLM_ASSISTED_STEPS,
    MAX_REGENERATIONS_PER_STEP,
    STEP_IDS,
    WIZARD_STEPS,
    WizardDraft,
    clone_from,
    discard_draft,
    is_valid_step,
    list_drafts,
    load_draft,
    materialize,
    next_step,
    prev_step,
    save_draft,
    slugify,
)


# ---------------------------------------------------------------------------
# Slug + step navigation
# ---------------------------------------------------------------------------


def test_slugify_lowercases_and_replaces_spaces():
    assert slugify("Windows Media Platform") == "windows-media-platform"


def test_slugify_strips_leading_and_trailing_hyphens():
    assert slugify("---Hello World!!!") == "hello-world"


def test_slugify_falls_back_when_empty():
    assert slugify("!!!") == "product"


def test_step_ids_match_wizard_steps():
    assert STEP_IDS == [s[0] for s in WIZARD_STEPS]


def test_is_valid_step():
    assert is_valid_step("identity")
    assert not is_valid_step("unknown")


def test_next_step_walks_forward():
    assert next_step("identity") == "scope"
    assert next_step("scope") == "taxonomy"


def test_next_step_returns_none_on_last():
    assert next_step("snippets") is None


def test_next_step_returns_first_when_unknown():
    assert next_step("bogus") == "identity"


def test_prev_step_walks_backward():
    assert prev_step("scope") == "identity"
    assert prev_step("identity") is None


# ---------------------------------------------------------------------------
# Draft persistence
# ---------------------------------------------------------------------------


def test_load_draft_returns_none_when_missing(tmp_path):
    assert load_draft(tmp_path, "no_such_slug") is None


def test_save_then_load_roundtrip(tmp_path):
    d = WizardDraft(
        slug="testproduct",
        display="Test Product",
        description="desc",
        scope_in="in", scope_out="out",
        areas=[{"id": "audio", "display": "Audio", "features": [{"id": "output", "display": "Output"}]}],
    )
    save_draft(tmp_path, d)
    loaded = load_draft(tmp_path, "testproduct")
    assert loaded is not None
    assert loaded.display == "Test Product"
    assert loaded.scope_in == "in"
    assert loaded.areas[0]["id"] == "audio"


def test_save_populates_timestamps(tmp_path):
    d = WizardDraft(slug="ts")
    save_draft(tmp_path, d)
    loaded = load_draft(tmp_path, "ts")
    assert loaded.created_at
    assert loaded.updated_at


def test_list_drafts_returns_sorted(tmp_path):
    for slug in ("zzz", "aaa", "mmm"):
        save_draft(tmp_path, WizardDraft(slug=slug))
    drafts = list_drafts(tmp_path)
    assert [d.slug for d in drafts] == ["aaa", "mmm", "zzz"]


def test_discard_draft_removes_file(tmp_path):
    save_draft(tmp_path, WizardDraft(slug="gone"))
    assert load_draft(tmp_path, "gone") is not None
    discard_draft(tmp_path, "gone")
    assert load_draft(tmp_path, "gone") is None


# ---------------------------------------------------------------------------
# Iteration cap
# ---------------------------------------------------------------------------


def test_regenerations_start_at_zero_and_are_within_cap():
    d = WizardDraft(slug="x")
    for step in LLM_ASSISTED_STEPS:
        assert d.can_regenerate(step)


def test_can_regenerate_becomes_false_at_cap():
    d = WizardDraft(slug="x")
    for _ in range(MAX_REGENERATIONS_PER_STEP):
        d.note_regeneration("scope")
    assert not d.can_regenerate("scope")


def test_regeneration_counts_are_per_step():
    d = WizardDraft(slug="x")
    for _ in range(MAX_REGENERATIONS_PER_STEP):
        d.note_regeneration("scope")
    # Different step still has full budget
    assert d.can_regenerate("taxonomy")


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------


def test_clone_copies_taxonomy_prompts_snippets(tmp_path):
    """Set up a fake source product; clone should carry over the three files."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "taxonomy.yaml").write_text(
        yaml.safe_dump({"areas": [{"id": "a1", "features": [{"id": "f1"}]}]}),
        encoding="utf-8",
    )
    (source / "prompts.yaml").write_text(
        yaml.safe_dump({"id": "src_id", "version": 3,
                        "relevance": {"system": "src_sys"}}),
        encoding="utf-8",
    )
    (source / "examples" / "positive").mkdir(parents=True)
    (source / "examples" / "positive" / "s1.yaml").write_text(
        yaml.safe_dump({"polarity": "positive_example", "title": "S1", "body": "body"}),
        encoding="utf-8",
    )

    draft = clone_from(source, slug="new", display="New", description="d")
    assert draft.cloned_from == "source"
    assert draft.areas == [{"id": "a1", "features": [{"id": "f1"}]}]
    # id + version stripped so new product starts fresh
    assert "id" not in draft.prompts and "version" not in draft.prompts
    assert draft.prompts["relevance"]["system"] == "src_sys"
    assert len(draft.snippets) == 1
    assert draft.snippets[0]["title"] == "S1"


def test_clone_missing_files_returns_empty_sections(tmp_path):
    source = tmp_path / "bare"
    source.mkdir()
    # Only taxonomy.yaml exists
    (source / "taxonomy.yaml").write_text(
        yaml.safe_dump({"areas": []}),
        encoding="utf-8",
    )
    draft = clone_from(source, slug="new", display="New", description="")
    assert draft.areas == []
    assert draft.prompts == {}
    assert draft.snippets == []


# ---------------------------------------------------------------------------
# Materialize
# ---------------------------------------------------------------------------


def test_materialize_writes_all_config_files(tmp_path):
    """Materialize a draft into an on-disk product directory."""
    products_dir = tmp_path

    def _fake_scaffold(slug, display, description):
        pdir = products_dir / slug
        pdir.mkdir()
        (pdir / "product.yaml").write_text(
            yaml.safe_dump({"id": slug, "display": display, "description": description}),
            encoding="utf-8",
        )
        (pdir / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
        (pdir / "taxonomy.yaml").write_text("areas: []\n", encoding="utf-8")

    draft = WizardDraft(
        slug="materialized", display="Test", description="d",
        areas=[{"id": "a", "display": "A", "features": [{"id": "f", "display": "F"}]}],
        prompts={"relevance": {"system": "s"}, "classify": {"system": "c"}},
        sources=[{"type": "hn", "id": "hn-1"}],
        snippets=[{"polarity": "positive_example", "title": "T", "body": "B"}],
    )
    save_draft(products_dir, draft)
    product_dir = materialize(draft, scaffold_fn=_fake_scaffold, products_dir=products_dir)

    assert (product_dir / "taxonomy.yaml").exists()
    tax = yaml.safe_load((product_dir / "taxonomy.yaml").read_text(encoding="utf-8"))
    assert tax["areas"][0]["id"] == "a"

    prm = yaml.safe_load((product_dir / "prompts.yaml").read_text(encoding="utf-8"))
    assert prm["relevance"]["system"] == "s"

    src = yaml.safe_load((product_dir / "sources.yaml").read_text(encoding="utf-8"))
    assert src["sources"][0]["type"] == "hn"

    snips = list((product_dir / "examples" / "positive").glob("*.yaml"))
    assert len(snips) == 1
    snip_body = yaml.safe_load(snips[0].read_text(encoding="utf-8"))
    assert snip_body["body"] == "B"

    # Draft removed after materialization
    assert load_draft(products_dir, "materialized") is None
