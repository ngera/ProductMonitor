"""Tests for pipeline/eval.py (POST_V1_PLAN §4.10)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pipeline.eval import (
    MIN_GOLDEN_SET_SIZE,
    EvalPerItem,
    _bootstrap_ci,
    _multilabel_counts,
    _prf,
    _sign,
    _sentiment_class,
    compute_summary,
    detect_regressions,
    resolve_cutoff_date,
    score_prediction,
)


# ---------------------------------------------------------------------------
# Metric primitives
# ---------------------------------------------------------------------------


def test_prf_all_zero():
    assert _prf(0, 0, 0) == {"precision": 0.0, "recall": 0.0, "f1": 0.0}


def test_prf_perfect():
    assert _prf(10, 0, 0) == {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_prf_partial():
    result = _prf(6, 2, 3)   # p = 6/8, r = 6/9, f1 = 2pr/(p+r)
    assert result["precision"] == round(6 / 8, 4)
    assert result["recall"] == round(6 / 9, 4)
    # F1 harmonic mean
    assert 0.68 < result["f1"] < 0.72


def test_multilabel_counts_disjoint():
    assert _multilabel_counts({"a", "b"}, {"c", "d"}) == (0, 2, 2)


def test_multilabel_counts_partial_overlap():
    tp, fp, fn = _multilabel_counts({"a", "b", "c"}, {"a", "d"})
    assert tp == 1 and fp == 2 and fn == 1


def test_sentiment_class_thresholds():
    assert _sentiment_class(-0.5) == "neg"
    assert _sentiment_class(-0.2) == "neg"
    assert _sentiment_class(-0.19) == "neutral"
    assert _sentiment_class(0.19) == "neutral"
    assert _sentiment_class(0.2) == "pos"
    assert _sentiment_class(None) == "neutral"


def test_sign_thresholds():
    assert _sign(0.06) == 1
    assert _sign(0.05) == 0
    assert _sign(-0.05) == 0
    assert _sign(-0.06) == -1
    assert _sign(None) == 0


# ---------------------------------------------------------------------------
# Bootstrap CI — deterministic given fixed seed
# ---------------------------------------------------------------------------


def test_bootstrap_ci_deterministic():
    """Same input + same seed → same interval, byte-stable."""
    data = [1.0] * 20 + [0.0] * 5
    lo1, hi1 = _bootstrap_ci(data, lambda s: sum(s) / len(s), seed=13, iterations=200)
    lo2, hi2 = _bootstrap_ci(data, lambda s: sum(s) / len(s), seed=13, iterations=200)
    assert (lo1, hi1) == (lo2, hi2)


def test_bootstrap_ci_all_same_gives_tight_interval():
    """All items identical → CI is a single point."""
    data = [1.0] * 100
    lo, hi = _bootstrap_ci(data, lambda s: sum(s) / len(s), seed=13, iterations=500)
    assert lo == hi == 1.0


def test_bootstrap_ci_empty_returns_zero():
    lo, hi = _bootstrap_ci([], lambda s: 0.0)
    assert lo == hi == 0.0


# ---------------------------------------------------------------------------
# score_prediction — bridge between classify output and eval
# ---------------------------------------------------------------------------


def _fake_prediction(**kwargs):
    return SimpleNamespace(
        areas=kwargs.get("areas", []),
        content_types=kwargs.get("content_types", []),
        entities=kwargs.get("entities", []),
        sentiment=kwargs.get("sentiment"),
        bug_severity=kwargs.get("bug_severity"),
    )


def _fake_regex(kb_numbers=()):
    return SimpleNamespace(kb_numbers=list(kb_numbers))


def test_score_prediction_perfect():
    pred = _fake_prediction(
        areas=["audio", "network"],
        content_types=["bug_report"],
        sentiment=-0.4,
        bug_severity="high",
    )
    r = score_prediction(
        snippet_id="s1",
        predicted=pred,
        regex_res=_fake_regex(),
        primary_area="audio",
        gold_labels={
            "areas": ["audio", "network"],
            "content_types": ["bug_report"],
            "sentiment": -0.5,
            "primary_area": "audio",
            "severity": "high",
        },
    )
    assert r.areas == (2, 0, 0)
    assert r.content_types == (1, 0, 0)
    assert r.primary_area_correct is True
    assert r.sentiment_3class_correct is True
    assert r.sentiment_sign_correct is True
    assert r.severity_correct is True


def test_score_prediction_missing_gold_fields_yield_none():
    """When gold lacks primary_area / sentiment / severity, per-item marks None."""
    pred = _fake_prediction(areas=["audio"], sentiment=None)
    r = score_prediction(
        snippet_id="s2",
        predicted=pred,
        regex_res=_fake_regex(),
        primary_area="audio",
        gold_labels={"areas": ["audio"]},
    )
    assert r.primary_area_correct is None
    assert r.sentiment_3class_correct is None
    assert r.severity_correct is None


def test_score_prediction_kb_case_insensitive():
    pred = _fake_prediction()
    r = score_prediction(
        snippet_id="s3",
        predicted=pred,
        regex_res=_fake_regex(kb_numbers=["kb5036980"]),
        primary_area="",
        gold_labels={"kb_numbers": ["KB5036980"]},
    )
    assert r.kb == (1, 0, 0)


# ---------------------------------------------------------------------------
# compute_summary
# ---------------------------------------------------------------------------


def _perfect_item(sid: str) -> EvalPerItem:
    return EvalPerItem(
        id=sid,
        areas=(2, 0, 0),
        content_types=(1, 0, 0),
        entities=(0, 0, 0),
        kb=(0, 0, 0),
        primary_area_correct=True,
        sentiment_3class_correct=True,
        sentiment_sign_correct=True,
        severity_correct=True,
    )


def test_compute_summary_insufficient_data():
    """Below the golden-set floor → status=insufficient_data, no metrics."""
    items = [_perfect_item(str(i)) for i in range(10)]
    summary = compute_summary(
        items,
        product_id="p", run_id="r",
        n_snippets_total=10, n_golden=10, cutoff_date=None,
    )
    assert summary.status == "insufficient_data"
    assert summary.metrics == {}


def test_compute_summary_ok_when_at_floor():
    items = [_perfect_item(str(i)) for i in range(MIN_GOLDEN_SET_SIZE)]
    summary = compute_summary(
        items,
        product_id="p", run_id="r",
        n_snippets_total=MIN_GOLDEN_SET_SIZE,
        n_golden=MIN_GOLDEN_SET_SIZE,
        cutoff_date=None,
    )
    assert summary.status == "ok"
    assert summary.metrics["areas"]["f1"] == 1.0
    assert summary.metrics["primary_area"]["accuracy"] == 1.0
    # Bootstrap CI is a 2-list on every metric
    assert len(summary.metrics["areas"]["f1_ci"]) == 2
    assert len(summary.metrics["primary_area"]["ci"]) == 2


def test_compute_summary_counts_errors():
    items = [_perfect_item(str(i)) for i in range(25)]
    items += [EvalPerItem(
        id=f"err{i}", areas=(0, 0, 0), content_types=(0, 0, 0),
        entities=(0, 0, 0), kb=(0, 0, 0),
        primary_area_correct=None, sentiment_3class_correct=None,
        sentiment_sign_correct=None, severity_correct=None, error="LLM timeout",
    ) for i in range(5)]
    summary = compute_summary(
        items,
        product_id="p", run_id="r",
        n_snippets_total=30, n_golden=30, cutoff_date=None,
    )
    assert summary.n_evaluated == 25
    assert summary.n_errors == 5


# ---------------------------------------------------------------------------
# Regression detection
# ---------------------------------------------------------------------------


def _metric(point: float, ci: tuple[float, float], kind: str = "accuracy") -> dict:
    if kind == "f1":
        return {"f1": point, "f1_ci": list(ci)}
    return {"accuracy": point, "ci": list(ci)}


def test_no_regression_when_history_empty():
    current = {"primary_area": _metric(0.5, (0.4, 0.6))}
    assert detect_regressions(current, []) == []


def test_no_regression_when_metric_stable():
    baseline = [{"primary_area": _metric(0.8, (0.7, 0.9))}] * 3
    current = {"primary_area": _metric(0.79, (0.7, 0.88))}
    assert detect_regressions(current, baseline) == []


def test_regression_flagged_on_large_drop():
    baseline = [{"primary_area": _metric(0.9, (0.85, 0.94))}] * 3
    # Drop of 15pp; CI upper (0.79) < baseline median (0.9).
    current = {"primary_area": _metric(0.75, (0.71, 0.79))}
    regressed = detect_regressions(current, baseline)
    assert regressed == ["primary_area"]


def test_no_regression_when_ci_upper_still_covers_baseline():
    baseline = [{"primary_area": _metric(0.9, (0.85, 0.94))}] * 3
    # Drop of 10pp but CI upper (0.92) >= baseline median (0.9)
    current = {"primary_area": _metric(0.80, (0.68, 0.92))}
    assert detect_regressions(current, baseline) == []


def test_regression_works_on_f1_metrics_too():
    baseline = [{"areas": _metric(0.85, (0.82, 0.88), kind="f1")}] * 3
    current = {"areas": _metric(0.70, (0.66, 0.74), kind="f1")}
    assert detect_regressions(current, baseline) == ["areas"]


# ---------------------------------------------------------------------------
# Cutoff-date resolution
# ---------------------------------------------------------------------------


def test_resolve_cutoff_from_explicit_iso_string():
    cutoff = resolve_cutoff_date({"eval": {"golden_set_cutoff_date": "2026-01-01"}})
    assert cutoff is not None
    assert cutoff.year == 2026 and cutoff.month == 1 and cutoff.day == 1


def test_resolve_cutoff_falls_back_to_six_months_ago():
    """No config → cutoff is roughly 6 months in the past."""
    cutoff = resolve_cutoff_date({})
    assert cutoff is not None
    now = datetime.now(timezone.utc)
    # Between 5 and 7 months ago accounts for the day-1 rounding + 30-day months.
    assert timedelta(days=140) < (now - cutoff) < timedelta(days=220)


def test_resolve_cutoff_malformed_falls_back_to_default():
    """Unparseable value → default, not a crash."""
    cutoff = resolve_cutoff_date({"eval": {"golden_set_cutoff_date": "not-a-date"}})
    assert cutoff is not None
