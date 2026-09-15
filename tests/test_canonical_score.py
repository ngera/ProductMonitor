"""Regression tests for group.canonical_score (finding #6.1).

Two bugs the prior version had:
  1. `item_score` was unbounded (~0.3-5); other terms are [0,1]. The
     weighted combination was dominated by item_score — repro quality
     and body length barely mattered.
  2. Engagement was counted TWICE — once inside item_score via
     `engagement_w`, and again as `eng * 0.2` in the outer combine.
"""

from __future__ import annotations

import pytest

pytest.importorskip("structlog")

from pipeline.group import canonical_score


def _item(score: float, repro: str = "none", body: str = "", eng: float = 0.0):
    return {
        "score": score,
        "repro_steps_quality": repro,
        "body": body,
        "engagement_score": eng,
    }


def test_high_repro_wins_when_scores_equal() -> None:
    """The fix: with normalized item_score, repro / body / (implicit)
    engagement can actually tilt the choice. Prior formula would have
    picked either item since item_score dominated."""
    a = _item(score=2.0, repro="none",     body="x" * 200)
    b = _item(score=2.0, repro="detailed", body="x" * 200)
    max_score = 2.0
    assert canonical_score(b, max_score) > canonical_score(a, max_score)


def test_longer_body_wins_when_score_and_repro_equal() -> None:
    a = _item(score=2.0, repro="partial", body="short")
    b = _item(score=2.0, repro="partial", body="x" * 4000)
    max_score = 2.0
    assert canonical_score(b, max_score) > canonical_score(a, max_score)


def test_score_still_matters_when_other_terms_equal() -> None:
    # Two items with identical repro + body but different scores — the
    # higher-scored one wins.
    a = _item(score=1.0, repro="detailed", body="x" * 1000)
    b = _item(score=3.0, repro="detailed", body="x" * 1000)
    max_score = 3.0
    assert canonical_score(b, max_score) > canonical_score(a, max_score)


def test_no_double_count_engagement() -> None:
    """`engagement_score` on the item dict must be IGNORED — the outer
    engagement term was removed to fix the double-count. Two items
    identical in every field except engagement_score must score equally."""
    a = _item(score=2.0, repro="detailed", body="x" * 500, eng=0.0)
    b = _item(score=2.0, repro="detailed", body="x" * 500, eng=1.0)
    max_score = 2.0
    assert canonical_score(a, max_score) == canonical_score(b, max_score), (
        "engagement_score in the outer combine is a double-count of "
        "score.engagement_w — the fix removed the outer term. If this "
        "test fails, the double-count is back."
    )


def test_scale_is_bounded() -> None:
    """With max_score supplied, canonical_score outputs are in [0, 1] —
    a proper convex combination of [0,1] terms with weights summing to 1."""
    for score in (0.0, 0.5, 1.5, 3.0, 100.0):
        for repro in ("none", "partial", "detailed"):
            for body_len in (0, 500, 2000, 4000, 10_000):
                item = _item(score=score, repro=repro, body="x" * body_len)
                val = canonical_score(item, max_item_score=max(score, 1.0))
                assert 0.0 <= val <= 1.0 + 1e-9, (
                    f"score={score} repro={repro} body_len={body_len} -> {val}"
                )


def test_zero_max_score_does_not_divide_by_zero() -> None:
    # Guard from the caller: gmax=0 → uses 1.0 fallback in run_group.
    # canonical_score itself must not crash even if max_item_score=0.
    item = _item(score=0.0, repro="detailed", body="x" * 200)
    val = canonical_score(item, max_item_score=0.0)
    assert 0.0 <= val <= 1.0
