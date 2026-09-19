"""ADR-0033: --from-stage gating and classify skip/retry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from pipeline.run import PIPELINE_STAGES, should_run_stage


def test_should_run_stage_none_runs_all():
    for s in PIPELINE_STAGES:
        assert should_run_stage(s, None) is True


def test_should_run_stage_skips_before_gate():
    assert should_run_stage("fetch", "classify") is False
    assert should_run_stage("normalize", "classify") is False
    assert should_run_stage("filter", "classify") is False
    assert should_run_stage("relevance", "classify") is False
    assert should_run_stage("classify", "classify") is True
    assert should_run_stage("score", "classify") is True
    assert should_run_stage("render", "classify") is True


def test_should_run_stage_unknown_raises():
    with pytest.raises(ValueError, match="unknown stage"):
        should_run_stage("fetch", "not_a_stage")


def test_classify_skips_already_classified(monkeypatch):
    from pipeline import classify as cl
    from pipeline.models import CoreClassification

    calls: list[str] = []

    class FakeClient:
        model = "test"

        def structured(self, system, prompt, schema):
            calls.append("llm")
            return {
                "is_topic_relevant": True,
                "content_types": ["bug"],
                "sentiment": "negative",
                "summary": "x",
                "areas": [],
                "entities": [],
                "features": [],
            }

    pending = [
        {"id": "need:1", "source": "rss", "title": "t", "body": "b",
         "filter_status": "passed", "relevance_score": 0.9},
    ]
    queries: list[str] = []

    def _query(sql, params=None):
        queries.append(sql)
        if "COUNT" in sql:
            return [{"n": 7}]
        return pending

    flushed_status: list[tuple[str, str]] = []
    monkeypatch.setattr(cl, "app_config", lambda: {"grouping": {}})
    monkeypatch.setattr(cl, "current_product", lambda: MagicMock(
        classification_schema={"type": "object"},
        prompts={},
        display="Acme",
        id="acme",
    ))
    monkeypatch.setattr(cl.storage, "query", _query)
    monkeypatch.setattr(cl.storage, "set_relevance_batch", lambda *a, **k: None)
    monkeypatch.setattr(
        cl.storage, "set_filter_status_batch",
        lambda rows: flushed_status.extend(rows),
    )
    monkeypatch.setattr(cl, "extract", lambda *a, **k: MagicMock(
        versions=[], builds=[], error_codes=[], urls=[],
    ))
    monkeypatch.setattr(cl, "_build_prompt", lambda *a, **k: ("sys", "user"))
    monkeypatch.setattr(cl, "normalize_classification", lambda raw, **k: (
        CoreClassification(
            is_topic_relevant=True,
            content_types=["bug"],
            sentiment=-0.5,
            summary="x",
            areas=[],
        ),
        MagicMock(conditional_violations=0),
    ))
    monkeypatch.setattr(cl, "choose_primary_area", lambda *a, **k: None)
    monkeypatch.setattr(cl, "_stage_persist", lambda *a, **k: None)
    monkeypatch.setattr(cl, "_flush_persist", lambda *a, **k: None)

    result = cl.run_classify("2026-W38", client=FakeClient())
    assert result["counters"]["skipped"] == 7
    assert result["counters"]["classified"] == 1
    assert len(calls) == 1
    assert any("classification_failed" in q for q in queries)
    assert ("need:1", "passed") in flushed_status


def test_classify_retries_classification_failed(monkeypatch):
    from pipeline import classify as cl

    class FakeClient:
        model = "test"

        def structured(self, system, prompt, schema):
            raise RuntimeError("boom")

    def _query(sql, params=None):
        if "COUNT" in sql:
            return [{"n": 0}]
        return [
            {"id": "fail:1", "source": "rss", "title": "t", "body": "b",
             "filter_status": "classification_failed", "relevance_score": 0.5},
        ]

    statuses: list[tuple[str, str]] = []
    monkeypatch.setattr(cl, "app_config", lambda: {"grouping": {}})
    monkeypatch.setattr(cl, "current_product", lambda: MagicMock(
        classification_schema=object,
    ))
    monkeypatch.setattr(cl.storage, "query", _query)
    monkeypatch.setattr(cl.storage, "set_relevance_batch", lambda *a, **k: None)
    monkeypatch.setattr(
        cl.storage, "set_filter_status_batch",
        lambda rows: statuses.extend(rows),
    )
    monkeypatch.setattr(cl, "extract", lambda *a, **k: MagicMock())
    monkeypatch.setattr(cl, "_build_prompt", lambda *a, **k: ("sys", "user"))

    result = cl.run_classify("2026-W38", client=FakeClient())
    assert result["counters"]["failed"] == 1
    assert ("fail:1", "classification_failed") in statuses


def test_infer_resume_stage_mid_classify():
    pytest.importorskip("fastapi")
    from webui.app import _infer_resume_stage

    states = [
        {"stage": "fetch", "status": "done"},
        {"stage": "normalize", "status": "done"},
        {"stage": "filter", "status": "done"},
        {"stage": "relevance", "status": "done"},
        {"stage": "classify", "status": "failed"},
        {"stage": "score", "status": "skipped"},
    ]
    assert _infer_resume_stage(states) == "classify"


def test_infer_resume_stage_after_last_done():
    pytest.importorskip("fastapi")
    from webui.app import _infer_resume_stage

    states = [
        {"stage": "fetch", "status": "done"},
        {"stage": "normalize", "status": "done"},
        {"stage": "filter", "status": "skipped"},
    ]
    assert _infer_resume_stage(states) == "filter"


def test_infer_resume_stage_all_done():
    pytest.importorskip("fastapi")
    from webui.app import _infer_resume_stage

    states = [
        {"stage": "fetch", "status": "done"},
        {"stage": "render", "status": "done"},
    ]
    assert _infer_resume_stage(states) is None


def test_parse_stage_states_from_stage_marks_prior_done():
    pytest.importorskip("fastapi")
    from webui.app import _parse_stage_states

    stdout = "\n".join([
        "event=from_stage stage=classify",
        "event=stage_start stage=classify",
    ])
    states = _parse_stage_states(stdout, run_complete=False)
    by = {s["stage"]: s["status"] for s in states}
    assert by["fetch"] == "done"
    assert by["normalize"] == "done"
    assert by["filter"] == "done"
    assert by["relevance"] == "done"
    assert by["classify"] == "running"
    assert by["score"] == "pending"


def test_inherit_prior_stages_copies_snapshots(tmp_path, monkeypatch):
    from pipeline import stage_capture as sc

    monkeypatch.setattr(sc, "temp_run_root", lambda pid: tmp_path / pid / "temp_runs")
    prior = tmp_path / "acme" / "temp_runs" / "prior"
    prior.mkdir(parents=True)
    (prior / "stages.json").write_text('{"stages": ["fetch", "filter"]}', encoding="utf-8")
    (prior / "fetch.jsonl").write_text(
        '{"source": "rss", "line_count": 3}\n', encoding="utf-8",
    )
    (prior / "fetch.meta.json").write_text('{"row_count": 3}', encoding="utf-8")
    (prior / "filter.jsonl").write_text(
        '{"source": "rss", "filter_status": "passed", "is_relevant": true}\n',
        encoding="utf-8",
    )

    copied = sc.inherit_prior_stages(
        "acme", "new", "prior", ["fetch", "normalize", "filter"],
    )
    assert "fetch" in copied
    assert "filter" in copied
    new = tmp_path / "acme" / "temp_runs" / "new"
    assert (new / "fetch.jsonl").exists()
    assert (new / "filter.jsonl").exists()
    idx = __import__("json").loads((new / "stages.json").read_text(encoding="utf-8"))
    assert idx["stages"] == ["fetch", "filter"]
