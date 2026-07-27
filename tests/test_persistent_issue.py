"""Unit tests for persistent_issue helper functions.

The stage's `run()` requires sentence-transformers + a warehouse. Those live
in an integration test we can add once CI has the model cached. These tests
cover the pure functions that decide section membership, canonical-text
preparation, and cosine similarity — the correctness of which is what
makes the clustering reproducible.
"""

from __future__ import annotations

import json

import pytest

from pipeline import persistent_issue as pi


def _row(*, content_types=(), sentiment=None, title="", summary=""):
    return {
        "content_types_json": json.dumps(list(content_types)),
        "sentiment": sentiment,
        "title": title,
        "summary": summary,
    }


class TestMatchesSection:
    def test_bugs_section_matches_bug_report(self):
        assert pi.matches_section(_row(content_types=["bug_report"]), "bugs")

    def test_bugs_section_matches_hybrid(self):
        # A bug+feature item lands in BOTH bugs and features (per §11 of design).
        row = _row(content_types=["bug_report", "feature_request"])
        assert pi.matches_section(row, "bugs")
        assert pi.matches_section(row, "features")

    def test_features_section_ignores_pure_bug(self):
        assert not pi.matches_section(_row(content_types=["bug_report"]), "features")

    def test_positive_section_needs_sentiment_above_threshold(self):
        # Default threshold is 0.2 per app.yaml.
        assert pi.matches_section(_row(sentiment=0.5), "positive")
        assert not pi.matches_section(_row(sentiment=0.15), "positive")
        assert not pi.matches_section(_row(sentiment=None), "positive")

    def test_negative_section_needs_sentiment_below_threshold(self):
        assert pi.matches_section(_row(sentiment=-0.5), "negative")
        assert not pi.matches_section(_row(sentiment=-0.1), "negative")
        assert not pi.matches_section(_row(sentiment=None), "negative")

    def test_neutral_sentiment_matches_neither_pos_nor_neg(self):
        assert not pi.matches_section(_row(sentiment=0.0), "positive")
        assert not pi.matches_section(_row(sentiment=0.0), "negative")

    def test_unknown_section_returns_false(self):
        assert not pi.matches_section(_row(content_types=["bug_report"]), "media")


class TestCanonicalText:
    def test_title_and_summary_concatenated(self):
        row = _row(title="Explorer crashes", summary="On right-click after KB5039212")
        assert pi.canonical_text(row) == "Explorer crashes. On right-click after KB5039212"

    def test_missing_summary_returns_title_only(self):
        row = _row(title="Explorer crashes", summary="")
        assert pi.canonical_text(row) == "Explorer crashes"

    def test_missing_both_returns_empty(self):
        assert pi.canonical_text(_row(title="", summary="")) == ""

    def test_truncates_to_1000_chars(self):
        row = _row(title="x" * 600, summary="y" * 600)
        assert len(pi.canonical_text(row)) == 1000

    def test_strips_whitespace(self):
        row = _row(title="  x  ", summary="  y  ")
        assert pi.canonical_text(row) == "x. y"


class TestCosineSimilarity:
    def test_identical_vectors_score_1(self):
        assert pi.cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_0(self):
        assert pi.cosine_similarity([1, 0], [0, 1]) == pytest.approx(0.0)

    def test_opposite_vectors_score_neg_1(self):
        assert pi.cosine_similarity([1, 0], [-1, 0]) == pytest.approx(-1.0)

    def test_zero_vector_returns_0(self):
        # No division by zero — degenerate case returns 0.
        assert pi.cosine_similarity([0, 0, 0], [1, 2, 3]) == 0.0

    def test_scale_invariant(self):
        # 2x a vector is the same direction.
        base = pi.cosine_similarity([1, 2, 3], [4, 5, 6])
        scaled = pi.cosine_similarity([2, 4, 6], [4, 5, 6])
        assert base == pytest.approx(scaled)
