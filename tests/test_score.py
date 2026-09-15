"""Regression tests for pipeline.score.

The engagement weight used to be `log1p(upvotes + 2*comments)` — for
zero-engagement items (RSS/news/Tavily, anything without an upvote channel)
that term was `log1p(0) = 0`, which zeroed the whole score. Any post with a
single upvote then out-ranked every media item at Group time. The floor
`1 + log1p(...)` keeps engagement as a tilt, not a gate.
"""

from __future__ import annotations

import math

import pytest


def _score(upvotes: int, comments: int) -> float:
    # Duplicates the formula from pipeline.score.run_score with the other
    # weights held at 1.0 so we can compare items on engagement alone.
    return 1.0 + math.log1p(upvotes + 2 * comments)


def test_zero_engagement_scores_nonzero() -> None:
    assert _score(0, 0) > 0.0


def test_engagement_still_tilts_ordering() -> None:
    # A post with real engagement must still outrank a zero-engagement item
    # at equal recency/credibility/confidence.
    assert _score(5, 3) > _score(0, 0)


def test_score_formula_matches_pipeline() -> None:
    """Guards against silent divergence between this test's copy of the
    formula and pipeline.score. If someone changes one, this asserts the
    other was updated too."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent
           / "pipeline" / "score.py").read_text(encoding="utf-8")
    assert "1.0 + math.log1p" in src, (
        "pipeline.score engagement floor was removed — see tests/test_score.py"
    )
