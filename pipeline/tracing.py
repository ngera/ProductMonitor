"""Pipeline span tracing (POST_V1_PLAN §4.16).

Lightweight OpenTelemetry-shaped spans written to
`data/<pid>/temp_runs/<run_id>/trace.jsonl`. One line per span:

    {"span_id": "abc123", "parent_span_id": "root", "name": "stage.filter",
     "start_ts": 1731123456.789, "end_ts": 1731123458.123,
     "duration_ms": 1334, "attrs": {"stage": "filter", "counters": {...}}}

Fields:
  span_id         random 8-hex identifier
  parent_span_id  parent span_id, or "root" for the top-level run span
  name            dot-separated name (e.g. "stage.classify", "llm.call.classify")
  start_ts        epoch seconds (float)
  end_ts          epoch seconds (float)
  duration_ms     int, computed as (end_ts - start_ts) * 1000
  attrs           arbitrary key-value metadata (dict)
  error           optional error string; set if the span raised

Usage:
    from pipeline.tracing import Tracer, span

    tracer = Tracer(run_dir)
    with tracer.span("stage.filter", stage="filter", n_items=42):
        do_the_work()

Design choices:
- Contextvars for parent tracking (async-correct; ADR-0005).
- No hard dependency on OpenTelemetry SDK — this is a simple JSONL writer.
  Later phases can add OTLP export if needed.
- Gated by `features.observability_traces_enabled`; when off, spans are
  no-ops (Tracer.enabled==False).
"""

from __future__ import annotations

import contextvars
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional


_current_span: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "current_span", default=None,
)


def _gen_span_id() -> str:
    """8 hex chars — enough uniqueness within a single run."""
    return os.urandom(4).hex()


class Tracer:
    """Writes spans as JSONL to `<trace_dir>/trace.jsonl`.

    Not thread-safe on the file handle — we open + append per span. Fine
    at pipeline volumes (~100s of spans per run).
    """

    def __init__(self, trace_dir: Path, enabled: bool = True) -> None:
        self.trace_dir = Path(trace_dir)
        self.enabled = enabled
        if self.enabled:
            self.trace_dir.mkdir(parents=True, exist_ok=True)
            self._trace_file = self.trace_dir / "trace.jsonl"

    def _write(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            with self._trace_file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            # Tracing is best-effort. Never let a trace-write failure crash
            # the pipeline.
            pass

    @contextmanager
    def span(self, name: str, **attrs: Any) -> Iterator[str]:
        """Emit a span. Sets `current_span` in contextvars so nested spans
        pick up parentage. Yields the span_id.

        Usage:
            with tracer.span("stage.filter", n_items=42) as sid:
                # ... do work ...
        """
        if not self.enabled:
            yield "disabled"
            return

        span_id = _gen_span_id()
        parent_id = _current_span.get() or "root"
        token = _current_span.set(span_id)
        start_ts = time.time()
        error: Optional[str] = None
        try:
            yield span_id
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raise
        finally:
            end_ts = time.time()
            _current_span.reset(token)
            record = {
                "span_id": span_id,
                "parent_span_id": parent_id,
                "name": name,
                "start_ts": round(start_ts, 6),
                "end_ts": round(end_ts, 6),
                "duration_ms": int((end_ts - start_ts) * 1000),
                "attrs": attrs,
            }
            if error is not None:
                record["error"] = error
            self._write(record)


def null_tracer() -> Tracer:
    """A tracer with enabled=False for callers that want to unconditionally
    have a `.span()` context manager available."""
    t = Tracer.__new__(Tracer)
    t.trace_dir = Path("/dev/null")
    t.enabled = False
    return t
