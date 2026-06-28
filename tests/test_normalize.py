"""Tests for normalize-stage derived fields (is_reply + author_intent)."""

from __future__ import annotations

from pipeline.normalize import _author_intent


def test_reply_always_user_reply():
    rec = {"source": "reddit", "parent_external_id": "abc123"}
    assert _author_intent(rec) == "user_reply"


def test_reply_overrides_editorial_source():
    # Even on a source whose top-level posts are editorial, a reply is still
    # a user_reply (someone responding to the editorial piece).
    rec = {"source": "microsoft_community", "parent_external_id": "p1",
           "raw": {"is_official_voice": True}}
    assert _author_intent(rec) == "user_reply"


def test_msc_official_voice_is_editorial():
    rec = {"source": "microsoft_community", "parent_external_id": None,
           "raw": {"is_official_voice": True}}
    assert _author_intent(rec) == "editorial"


def test_msc_community_voice_is_user_original():
    rec = {"source": "microsoft_community", "parent_external_id": None,
           "raw": {"is_official_voice": False}}
    assert _author_intent(rec) == "user_original"


def test_reddit_top_level_is_user_original():
    rec = {"source": "reddit", "parent_external_id": None}
    assert _author_intent(rec) == "user_original"


def test_hn_story_is_user_original():
    rec = {"source": "hn", "parent_external_id": None}
    assert _author_intent(rec) == "user_original"


def test_github_issue_is_user_original():
    rec = {"source": "github_issues", "parent_external_id": None}
    assert _author_intent(rec) == "user_original"


def test_unknown_source_defaults_to_user_original():
    rec = {"source": "made_up_source", "parent_external_id": None}
    assert _author_intent(rec) == "user_original"
