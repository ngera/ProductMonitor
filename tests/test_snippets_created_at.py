"""Snippet migration tests (POST_V1_PLAN §4.10):

- created_at parsed from YAML when present
- backfilled from file mtime when absent
- golden_set_subset / training_subset use created_at + cutoff
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from pipeline.snippets import (
    NEGATIVE,
    POSITIVE,
    Snippet,
    golden_set_subset,
    load_snippets,
    training_subset,
)


def _write_snippet(dir_: Path, name: str, polarity_dir: str, body: str, created_at: str | None = None) -> Path:
    (dir_ / "examples" / polarity_dir).mkdir(parents=True, exist_ok=True)
    payload = {
        "title": name,
        "body": body,
        "polarity": POSITIVE if polarity_dir == "positive" else NEGATIVE,
        "labels": {"areas": ["audio"]},
    }
    if created_at is not None:
        payload["created_at"] = created_at
    path = dir_ / "examples" / polarity_dir / f"{name}.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


def test_created_at_from_explicit_iso(tmp_path):
    _write_snippet(tmp_path, "explicit", "positive", "body text",
                   created_at="2024-06-15T00:00:00+00:00")
    snippets = load_snippets(tmp_path)
    assert len(snippets) == 1
    s = snippets[0]
    assert s.created_at is not None
    assert s.created_at.year == 2024 and s.created_at.month == 6 and s.created_at.day == 15
    assert s.created_at.tzinfo is not None


def test_created_at_backfilled_from_mtime(tmp_path):
    """Missing created_at → we use the file's mtime."""
    path = _write_snippet(tmp_path, "no_ts", "positive", "body")
    # Force a specific mtime — one year ago
    one_year_ago = (datetime.now(timezone.utc) - timedelta(days=365)).timestamp()
    os.utime(path, (one_year_ago, one_year_ago))

    snippets = load_snippets(tmp_path)
    assert snippets[0].created_at is not None
    # Within a small tolerance of one_year_ago
    delta = abs(snippets[0].created_at.timestamp() - one_year_ago)
    assert delta < 5  # seconds


def test_created_at_iso_without_tz_is_treated_as_utc(tmp_path):
    _write_snippet(tmp_path, "naive", "positive", "body", created_at="2025-03-20")
    s = load_snippets(tmp_path)[0]
    assert s.created_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Time-based split
# ---------------------------------------------------------------------------


def _snip(sid: str, created_at: datetime, polarity: str = POSITIVE) -> Snippet:
    return Snippet(
        id=sid,
        polarity=polarity,
        source_url=None,
        title=sid,
        body="body",
        labels={},
        holdout_eval=False,
        notes="",
        path=None,
        created_at=created_at,
    )


def test_golden_set_subset_splits_on_cutoff():
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old = _snip("old", datetime(2025, 6, 1, tzinfo=timezone.utc))
    at_cutoff = _snip("at_cutoff", cutoff)
    fresh = _snip("fresh", datetime(2026, 5, 1, tzinfo=timezone.utc))

    golden = golden_set_subset([old, at_cutoff, fresh], cutoff)
    training = training_subset([old, at_cutoff, fresh], cutoff)

    golden_ids = {s.id for s in golden}
    training_ids = {s.id for s in training}
    assert golden_ids == {"old", "at_cutoff"}   # boundary belongs to golden (<=)
    assert training_ids == {"fresh"}


def test_golden_set_subset_no_cutoff_returns_all():
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snippets = [_snip("a", dt), _snip("b", dt)]
    assert len(golden_set_subset(snippets, None)) == 2
    assert training_subset(snippets, None) == []


def test_golden_set_skips_snippets_without_created_at():
    """created_at defaults to None only when loaded from a malformed source;
    in that case we exclude from the golden set to avoid arbitrary inclusion."""
    cutoff = datetime(2026, 1, 1, tzinfo=timezone.utc)
    snippets = [
        _snip("ok", datetime(2025, 6, 1, tzinfo=timezone.utc)),
        Snippet(id="missing", polarity=POSITIVE, source_url=None, title=None,
                body="body", labels={}, created_at=None),
    ]
    golden = golden_set_subset(snippets, cutoff)
    assert [s.id for s in golden] == ["ok"]
