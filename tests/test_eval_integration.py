"""Integration tests for the eval stage (POST_V1_PLAN §4.10).

- End-to-end: run_eval with a fixture product produces an eval_summary.json
- Machine-readable output is stable (regression golden-file)
- Acceptance gate integration with render.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pipeline import eval as _eval
from pipeline.eval import EvalPerItem, EvalSummary


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


# ---------------------------------------------------------------------------
# _write_summary / load_summary roundtrip
# ---------------------------------------------------------------------------


def test_write_and_load_summary_roundtrip(tmp_path, monkeypatch):
    """`_write_summary` persists exactly what `load_summary` returns."""
    from pipeline import eval as ev

    def fake_path(pid, rid):
        return tmp_path / pid / "temp_runs" / rid / "eval_summary.json"

    monkeypatch.setattr(ev, "_summary_path", fake_path)

    summary = EvalSummary(
        status="ok",
        product_id="prod",
        run_id="run1",
        n_snippets_total=50,
        n_golden=32,
        n_evaluated=32,
        n_errors=0,
        cutoff_date="2026-01-01T00:00:00+00:00",
        metrics={"primary_area": {"accuracy": 0.9, "ci": [0.85, 0.95], "n": 30}},
        regressions=[],
        generated_at="2026-07-01T00:00:00+00:00",
    )
    ev._write_summary(summary)
    loaded = ev.load_summary("prod", "run1")
    assert loaded is not None
    assert loaded["status"] == "ok"
    assert loaded["n_golden"] == 32
    assert loaded["metrics"]["primary_area"]["accuracy"] == 0.9


def test_load_summary_returns_none_when_missing(tmp_path, monkeypatch):
    from pipeline import eval as ev
    monkeypatch.setattr(ev, "_summary_path", lambda p, r: tmp_path / "missing.json")
    assert ev.load_summary("p", "r") is None


# ---------------------------------------------------------------------------
# Machine-readable output is stable (deterministic given fixed seed)
# ---------------------------------------------------------------------------


def test_summary_output_is_stable_given_same_input():
    """Same per-item scores → same summary JSON. Guards against silent
    changes in the bootstrap seed / rounding / dict ordering."""
    items = [_perfect_item(f"s{i}") for i in range(30)]
    s1 = _eval.compute_summary(
        items,
        product_id="p", run_id="r",
        n_snippets_total=30, n_golden=30, cutoff_date=None,
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    s2 = _eval.compute_summary(
        items,
        product_id="p", run_id="r",
        n_snippets_total=30, n_golden=30, cutoff_date=None,
        generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert json.dumps(s1.to_dict(), sort_keys=True) == json.dumps(s2.to_dict(), sort_keys=True)


# ---------------------------------------------------------------------------
# _load_recent_history — only "ok" summaries, oldest-first, capped at N
# ---------------------------------------------------------------------------


def test_load_recent_history_returns_ok_only_ordered(tmp_path, monkeypatch):
    from pipeline import eval as ev

    def fake_path(pid, rid):
        return tmp_path / pid / "temp_runs" / rid / "eval_summary.json"

    monkeypatch.setattr(ev, "_summary_path", fake_path)

    def fake_config():
        return {"paths": {"data_root": str(tmp_path)}}
    monkeypatch.setattr("pipeline.config.app_config", fake_config)
    monkeypatch.setattr("pipeline.config.resolve_path", lambda x: Path(x))

    # Older ok, newer insufficient, newest ok, plus a not-yet-complete run
    for rid, gen, status, acc in [
        ("run_old_ok",   "2026-01-01T00:00:00+00:00", "ok", 0.7),
        ("run_bad",      "2026-02-01T00:00:00+00:00", "insufficient_data", None),
        ("run_new_ok",   "2026-03-01T00:00:00+00:00", "ok", 0.85),
        ("run_current",  "2026-04-01T00:00:00+00:00", "ok", 0.5),
    ]:
        metrics = {"primary_area": {"accuracy": acc, "ci": [acc - 0.05, acc + 0.05], "n": 30}} if acc is not None else {}
        summary = EvalSummary(
            status=status, product_id="p", run_id=rid,
            n_snippets_total=30, n_golden=30, n_evaluated=30, n_errors=0,
            cutoff_date=None, metrics=metrics, generated_at=gen,
        )
        ev._write_summary(summary)

    history = ev._load_recent_history("p", exclude_run_id="run_current", limit=3)
    # Only "ok" runs, oldest first, "run_current" excluded
    assert [h["primary_area"]["accuracy"] for h in history] == [0.7, 0.85]


# ---------------------------------------------------------------------------
# Acceptance gate integration with render.py (D9)
# ---------------------------------------------------------------------------


def test_acceptance_gate_off_returns_none(tmp_path, monkeypatch):
    """When product.yaml has no eval.acceptance_gate, gate check is None."""
    from pipeline import render

    fake_product = SimpleNamespace(product_meta={})
    monkeypatch.setattr("pipeline.config.current_product", lambda: fake_product)

    assert render._check_acceptance_gate("p", "r") is None


def test_acceptance_gate_below_threshold_fires(tmp_path, monkeypatch):
    """Gate on + metric below threshold → returns a failure dict."""
    from pipeline import eval as ev, render

    def fake_path(pid, rid):
        return tmp_path / pid / "temp_runs" / rid / "eval_summary.json"
    monkeypatch.setattr(ev, "_summary_path", fake_path)

    fake_product = SimpleNamespace(product_meta={
        "eval": {
            "acceptance_gate": True,
            "thresholds": {"primary_area": 0.80},
        }
    })
    monkeypatch.setattr("pipeline.config.current_product", lambda: fake_product)

    # Persist a summary with primary_area accuracy=0.72 (below threshold=0.80)
    summary = EvalSummary(
        status="ok", product_id="p", run_id="r",
        n_snippets_total=30, n_golden=30, n_evaluated=30, n_errors=0,
        cutoff_date=None,
        metrics={"primary_area": {"accuracy": 0.72, "ci": [0.65, 0.78], "n": 30}},
        generated_at="2026-07-01",
    )
    ev._write_summary(summary)

    result = render._check_acceptance_gate("p", "r")
    assert result is not None
    assert any(f["metric"] == "primary_area" and f["reason"] == "below threshold"
               for f in result["failures"])


def test_acceptance_gate_regression_fires(tmp_path, monkeypatch):
    """Gate on + regressions in summary → failure dict cites them."""
    from pipeline import eval as ev, render

    def fake_path(pid, rid):
        return tmp_path / pid / "temp_runs" / rid / "eval_summary.json"
    monkeypatch.setattr(ev, "_summary_path", fake_path)

    fake_product = SimpleNamespace(product_meta={
        "eval": {"acceptance_gate": True, "thresholds": {}}
    })
    monkeypatch.setattr("pipeline.config.current_product", lambda: fake_product)

    summary = EvalSummary(
        status="ok", product_id="p", run_id="r",
        n_snippets_total=30, n_golden=30, n_evaluated=30, n_errors=0,
        cutoff_date=None,
        metrics={"areas": {"f1": 0.75, "f1_ci": [0.70, 0.80], "n": 30}},
        regressions=["areas"],
        generated_at="2026-07-01",
    )
    ev._write_summary(summary)

    result = render._check_acceptance_gate("p", "r")
    assert result is not None
    assert any(f["metric"] == "areas" and f["reason"] == "regressed vs rolling median"
               for f in result["failures"])


def test_acceptance_gate_passes_when_all_metrics_above_threshold(tmp_path, monkeypatch):
    from pipeline import eval as ev, render

    def fake_path(pid, rid):
        return tmp_path / pid / "temp_runs" / rid / "eval_summary.json"
    monkeypatch.setattr(ev, "_summary_path", fake_path)

    fake_product = SimpleNamespace(product_meta={
        "eval": {"acceptance_gate": True, "thresholds": {"primary_area": 0.70}}
    })
    monkeypatch.setattr("pipeline.config.current_product", lambda: fake_product)

    summary = EvalSummary(
        status="ok", product_id="p", run_id="r",
        n_snippets_total=30, n_golden=30, n_evaluated=30, n_errors=0,
        cutoff_date=None,
        metrics={"primary_area": {"accuracy": 0.90, "ci": [0.85, 0.94], "n": 30}},
        regressions=[],
        generated_at="2026-07-01",
    )
    ev._write_summary(summary)
    assert render._check_acceptance_gate("p", "r") is None


def test_gate_failure_page_html_renders():
    """The standalone gate-failure HTML doesn't depend on a Jinja env."""
    from pipeline.render import _render_gate_failure_page
    html = _render_gate_failure_page(
        env=None, week_id="2026-W28", now="2026-07-16",
        gate={
            "run_id": "r1", "product_id": "p", "summary_ref": "/runs/r1",
            "failures": [{"metric": "primary_area", "value": 0.5,
                          "threshold": 0.8, "reason": "below threshold"}],
        },
    )
    assert "primary_area" in html
    assert "below threshold" in html
    assert "gated" in html.lower()


# ---------------------------------------------------------------------------
# run_eval feature-flag gate
# ---------------------------------------------------------------------------


def test_run_eval_no_op_when_flag_off(monkeypatch):
    """features.evals_enabled=False → run_eval returns without doing work."""
    monkeypatch.setattr("pipeline.features.enabled",
                        lambda flag, product_id=None: False)
    fake_product = SimpleNamespace(id="p", snippets=[], product_meta={})
    monkeypatch.setattr("pipeline.config.current_product", lambda: fake_product)

    result = _eval.run_eval("run_x")
    assert result["summary"] is None
    assert result["counters"] == {"evaluated": 0, "errors": 0}
