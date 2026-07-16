"""Tests for pipeline/tracing.py (POST_V1_PLAN §4.16)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.tracing import Tracer, null_tracer


def test_span_writes_record(tmp_path: Path):
    tracer = Tracer(tmp_path, enabled=True)
    with tracer.span("test.op", key="value"):
        pass

    trace_file = tmp_path / "trace.jsonl"
    assert trace_file.exists()
    lines = trace_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["name"] == "test.op"
    assert record["attrs"] == {"key": "value"}
    assert record["parent_span_id"] == "root"
    assert record["duration_ms"] >= 0
    assert record["span_id"]


def test_nested_spans_track_parent(tmp_path: Path):
    tracer = Tracer(tmp_path, enabled=True)
    with tracer.span("outer"):
        with tracer.span("inner"):
            pass

    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    records = [json.loads(l) for l in lines]
    # Inner span writes first (finishes first). Its parent should be outer's span_id.
    inner = next(r for r in records if r["name"] == "inner")
    outer = next(r for r in records if r["name"] == "outer")
    assert inner["parent_span_id"] == outer["span_id"]
    assert outer["parent_span_id"] == "root"


def test_error_recorded_in_span(tmp_path: Path):
    tracer = Tracer(tmp_path, enabled=True)
    with pytest.raises(ValueError):
        with tracer.span("failing.op"):
            raise ValueError("boom")

    lines = (tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
    record = json.loads(lines[0])
    assert record["name"] == "failing.op"
    assert "boom" in record["error"]
    assert record["error"].startswith("ValueError:")


def test_disabled_tracer_writes_nothing(tmp_path: Path):
    tracer = Tracer(tmp_path, enabled=False)
    with tracer.span("test.op"):
        pass
    # No file created
    assert not (tmp_path / "trace.jsonl").exists()


def test_null_tracer_is_context_manager_compatible():
    """null_tracer() gives callers a zero-cost tracer to use unconditionally."""
    tracer = null_tracer()
    assert tracer.enabled is False
    with tracer.span("no.op"):
        pass  # doesn't crash, doesn't write


def test_sibling_spans_share_same_parent(tmp_path: Path):
    """Two sibling spans within an outer span both point at outer."""
    tracer = Tracer(tmp_path, enabled=True)
    with tracer.span("outer"):
        with tracer.span("child_a"):
            pass
        with tracer.span("child_b"):
            pass

    records = [
        json.loads(l)
        for l in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
    ]
    outer = next(r for r in records if r["name"] == "outer")
    child_a = next(r for r in records if r["name"] == "child_a")
    child_b = next(r for r in records if r["name"] == "child_b")
    assert child_a["parent_span_id"] == outer["span_id"]
    assert child_b["parent_span_id"] == outer["span_id"]
