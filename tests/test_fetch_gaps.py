"""Digest banner for streams that came back empty (pipeline/digest/fetch_gaps.py)."""

from __future__ import annotations

from pathlib import Path

import jinja2

from pipeline.digest.fetch_gaps import fetch_gaps

SOURCES = [
    {"id": "reddit_rss", "streams": [
        {"name": "reddit_rss-supabase-2", "subreddit": "webdev"},
        {"name": "reddit_rss-supabase-3", "subreddit": "r/learnprogramming"},
        {"name": "reddit_rss-supabase-5", "subreddit": "rust"},
    ]},
    {"id": "rss", "streams": [
        {"name": "rss-supabase-3", "feed_url": "https://www.wired.com/feed/rss", "display": "WIRED"},
    ]},
]


def test_groups_failed_streams_by_reason_with_reader_facing_names():
    completeness = {"ceiling_hits": [
        ["rss:reddit_rss-supabase-2:rate-limited", 0.0],
        ["rss:reddit_rss-supabase-3:rate-limited", 0.0],
        ["rss:reddit_rss-supabase-5:rate-limited", 0.0],
        ["rss:rss-supabase-3:dead", 0.0],
        ["reddit:Windows11", 3600.0],   # a real page-ceiling hit: not a gap
    ]}
    gaps = fetch_gaps(completeness, SOURCES)
    assert [g["reason"] for g in gaps] == ["rate-limited", "dead"]
    assert gaps[0]["streams"] == ["r/learnprogramming", "r/rust", "r/webdev"]
    assert "HTTP 429" in gaps[0]["what"]
    assert gaps[1]["streams"] == ["WIRED"]


def test_no_gaps_when_everything_fetched():
    assert fetch_gaps({"ceiling_hits": []}, SOURCES) == []
    assert fetch_gaps(None, SOURCES) == []


def test_unknown_stream_falls_back_to_its_name():
    gaps = fetch_gaps({"ceiling_hits": [["rss:mystery:network", 0.0]]}, [])
    assert gaps[0]["streams"] == ["mystery"]


def test_index_template_shows_the_banner():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(Path(__file__).resolve().parent.parent / "report_templates")))
    src = env.loader.get_source(env, "digest_index.html.j2")[0]
    start = src.index("{% if fetch_gaps %}")
    end = src.index("{% endif %}", src.index("{% endfor %}", start)) + len("{% endif %}")
    html = env.from_string(src[start:end]).render(fetch_gaps=fetch_gaps(
        {"ceiling_hits": [["rss:reddit_rss-supabase-2:rate-limited", 0.0]]}, SOURCES))
    assert "Missing sources this week" in html
    assert "1 stream rate-limited (HTTP 429)" in html
    assert "r/webdev" in html
