"""Tests for the Phase 5 snippets module — loader, few-shot, holdout split."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.snippets import (
    NEGATIVE,
    POSITIVE,
    Snippet,
    delete_snippet,
    few_shot_subset,
    holdout_subset,
    load_snippets,
    render_classify_few_shot,
    render_relevance_few_shot,
    save_snippet,
    slugify,
)


def _mk(id: str, polarity: str, holdout: bool = False, body: str = "x" * 10) -> Snippet:
    return Snippet(
        id=id,
        polarity=polarity,
        source_url=None,
        title=f"title-{id}",
        body=body,
        labels={"is_topic_relevant": polarity == POSITIVE},
        holdout_eval=holdout,
    )


def test_slugify_strips_garbage():
    assert slugify("Hello, World!") == "hello-world"
    assert slugify("KB5036980 Bluetooth") == "kb5036980-bluetooth"
    assert slugify("---") == "snippet"


def test_few_shot_subset_excludes_holdouts_by_default():
    s = [
        _mk("p1", POSITIVE),
        _mk("p2", POSITIVE),
        _mk("p_holdout", POSITIVE, holdout=True),
        _mk("n1", NEGATIVE),
        _mk("n_holdout", NEGATIVE, holdout=True),
    ]
    picked = few_shot_subset(s, n_positive=5, n_negative=5)
    ids = {x.id for x in picked}
    assert "p_holdout" not in ids
    assert "n_holdout" not in ids
    assert ids == {"p1", "p2", "n1"}


def test_few_shot_subset_is_deterministic_for_a_seed():
    s = [_mk(f"p{i}", POSITIVE) for i in range(10)]
    a = few_shot_subset(s, n_positive=3, n_negative=0, seed=7)
    b = few_shot_subset(s, n_positive=3, n_negative=0, seed=7)
    assert [x.id for x in a] == [x.id for x in b]


def test_few_shot_caps_at_pool_size():
    s = [_mk("p1", POSITIVE)]
    picked = few_shot_subset(s, n_positive=10, n_negative=10)
    assert len(picked) == 1


def test_holdout_subset_returns_only_held():
    s = [
        _mk("p1", POSITIVE),
        _mk("p_held", POSITIVE, holdout=True),
        _mk("n_held", NEGATIVE, holdout=True),
    ]
    h = holdout_subset(s)
    assert {x.id for x in h} == {"p_held", "n_held"}


def test_render_blocks_empty_when_no_picks():
    assert render_relevance_few_shot([]) == ""
    assert render_classify_few_shot([]) == ""


def test_render_blocks_include_labels():
    picked = [_mk("p1", POSITIVE), _mk("n1", NEGATIVE)]
    r = render_relevance_few_shot(picked)
    assert "relevant: true" in r
    assert "relevant: false" in r
    c = render_classify_few_shot(picked)
    assert "is_topic_relevant" in c


def test_save_load_round_trip(tmp_path: Path):
    topic_dir = tmp_path / "t"
    s = Snippet(
        id="round-trip",
        polarity=POSITIVE,
        source_url="https://example.com/x",
        title="A title",
        body="A body",
        labels={"is_topic_relevant": True, "areas": ["audio"]},
        holdout_eval=True,
        notes="round trip test",
    )
    save_snippet(topic_dir, s)
    loaded = load_snippets(topic_dir)
    assert len(loaded) == 1
    got = loaded[0]
    assert got.id == "round-trip"
    assert got.polarity == POSITIVE
    assert got.title == "A title"
    assert got.body == "A body"
    assert got.holdout_eval is True
    assert got.labels["areas"] == ["audio"]


def test_delete_snippet_removes_file(tmp_path: Path):
    topic_dir = tmp_path / "t"
    s = _mk("to-delete", POSITIVE)
    path = save_snippet(topic_dir, s)
    s.path = path
    assert path.exists()
    delete_snippet(s)
    assert not path.exists()


def test_load_skips_broken_yaml_but_records_error(tmp_path: Path):
    topic_dir = tmp_path / "t"
    pos = topic_dir / "examples" / "positive"
    pos.mkdir(parents=True)
    # Body-less + URL-less is invalid per the loader contract.
    (pos / "broken.yaml").write_text("polarity: positive_example\n", encoding="utf-8")
    # Also a valid one to make sure the broken one didn't poison the iterator.
    save_snippet(topic_dir, _mk("good", POSITIVE))
    loaded = load_snippets(topic_dir)
    ids = {s.id: s for s in loaded}
    assert "good" in ids
    assert "broken" in ids
    assert "PARSE ERROR" in ids["broken"].body


def test_invalid_polarity_save_rejected(tmp_path: Path):
    topic_dir = tmp_path / "t"
    s = _mk("bad", "weird_polarity")
    with pytest.raises(ValueError):
        save_snippet(topic_dir, s)
