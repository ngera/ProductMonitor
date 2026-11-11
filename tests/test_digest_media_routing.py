"""Digest press block and summary totals.

Regression tests for two digests that showed the wrong items:

- Cursor 2026-W40: a Reddit post the classifier tagged `news_discussion`
  appeared under a legacy "Media Coverage — siloed from totals" section while
  also being counted in the totals. That section is gone (ADR-0035); the
  ADR-0028 press block, keyed on the source's `content_type`, is the one
  place press is listed.
- Supabase 2026-W39: an off-topic article whose classification failed still
  counted as a unique item.

The section queries are plain SQL, so they run here against an in-memory
SQLite database with the columns they read.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from pipeline.digest import sections

WEEK = "2026-W40"


@pytest.fixture
def db(monkeypatch):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE items (
            id TEXT PRIMARY KEY, url TEXT, title TEXT, body TEXT,
            created_at TEXT, week_id TEXT, source_display_name TEXT,
            is_relevant BOOLEAN, filter_status TEXT,
            content_type TEXT NOT NULL DEFAULT 'user_feedback'
        );
        CREATE TABLE item_classifications (
            item_id TEXT PRIMARY KEY, content_types_json TEXT,
            sentiment REAL, churn_signal BOOLEAN, summary TEXT
        );
        CREATE TABLE scores (item_id TEXT PRIMARY KEY, score REAL);
        """
    )

    def query(sql, params=None):
        return [dict(r) for r in con.execute(sql, params or []).fetchall()]

    monkeypatch.setattr(sections.storage, "query", query)
    return con


def _add(con, item_id, *, content_type, types=(), sentiment=0.0,
         filter_status="passed", title="", classified=True):
    con.execute(
        "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?)",
        [item_id, f"https://example.test/{item_id}", title or item_id, "",
         "2026-09-29", WEEK, "src", True, filter_status, content_type],
    )
    if classified:
        con.execute(
            "INSERT INTO item_classifications VALUES (?,?,?,?,?)",
            [item_id, json.dumps(list(types)), sentiment, False, "summary"],
        )
    con.execute("INSERT INTO scores VALUES (?,?)", [item_id, 1.0])


def test_press_block_lists_media_items_only(db):
    _add(db, "reddit-1", content_type="user_feedback", types=["news_discussion"],
         title="GLM 5.3 and 5.3 Flash available in Cursor")
    _add(db, "verge-1", content_type="media_coverage", types=[],
         title="Cursor raises a new round")

    press = sections.press_coverage_items(WEEK, top_n=5)

    assert [i["id"] for i in press] == ["verge-1"]


def test_press_block_skips_failed_classification(db):
    _add(db, "ok", content_type="media_coverage", title="Supabase launches X")
    _add(db, "failed", content_type="media_coverage",
         filter_status="classification_failed")

    press = sections.press_coverage_items(WEEK, top_n=5)

    assert [i["id"] for i in press] == ["ok"]


def test_summary_counts_skip_failed_items_and_keep_media(db):
    # ADR-0028: totals cover every item in the digest once, press included.
    _add(db, "fb-pos", content_type="user_feedback", types=["praise"], sentiment=0.7)
    _add(db, "fb-bug", content_type="user_feedback", types=["bug_report"], sentiment=-0.5)
    _add(db, "press", content_type="media_coverage", types=[], sentiment=0.8)
    _add(db, "failed", content_type="media_coverage",
         filter_status="classification_failed", classified=False)

    counts = sections.summary_counts(WEEK, {"positive": 0.2, "negative": -0.2})

    assert counts["unique_items"] == 3
    assert counts["positive"] == 2
    assert counts["bugs"] == 1


def test_legacy_media_section_is_gone():
    assert not hasattr(sections, "media_coverage_items")
