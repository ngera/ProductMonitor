"""UTF-8 stdio preflight and safe error text for Windows consoles."""

from __future__ import annotations

from pipeline.preflight import _force_utf8_stdio
from pipeline.util import safe_error_text


def test_safe_error_text_keeps_ascii_and_truncates():
    assert "boom" in safe_error_text(RuntimeError("boom"))
    long = "x" * 2000
    assert len(safe_error_text(long, limit=100)) <= 100


def test_safe_error_text_survives_emoji():
    # The char that aborted ui-20260920T131801-ef1b6e on cp1252.
    msg = "validation failed: summary='\U0001f914 maybe wifi'"
    out = safe_error_text(msg)
    assert "validation failed" in out
    # Must be encodable as cp1252 with replace (what a bad console would do)
    out.encode("cp1252", errors="replace")


def test_force_utf8_stdio_idempotent():
    _force_utf8_stdio()
    _force_utf8_stdio()
