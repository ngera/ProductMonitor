"""Run-detail per-source funnel table expands RSS into one row per stream."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def temp_run(tmp_path, monkeypatch):
    product_id = "acme"
    run_id = "ui-test-run"
    run_dir = tmp_path / "temp_runs" / run_id
    run_dir.mkdir(parents=True)

    monkeypatch.setattr("webui.app._temp_run_dir", lambda pid, rid: run_dir)

    # Product streams: three catalog pubs + two reddit subs.
    from types import SimpleNamespace
    product = SimpleNamespace(sources=[
        {
            "id": "rss",
            "type": "rss",
            "streams": [
                {"name": "rss-acme",
                 "feed_url": "https://techcrunch.com/feed/"},
                {"name": "rss-acme-2",
                 "feed_url": "https://www.theverge.com/rss/index.xml"},
                {"name": "rss-acme-3",
                 "feed_url": "https://www.wired.com/feed/rss"},
            ],
        },
        {
            "id": "reddit_rss",
            "type": "reddit_rss",
            "streams": [
                {"name": "reddit_rss-acme", "subreddit": "supabase"},
                {"name": "reddit_rss-acme-2", "subreddit": "webdev"},
            ],
        },
    ])
    monkeypatch.setattr("pipeline.product.load_product", lambda pid: product)
    monkeypatch.setattr(
        "pipeline.minifetch._media_catalog_by_url",
        lambda: {
            "https://techcrunch.com/feed/": "TechCrunch",
            "https://www.theverge.com/rss/index.xml": "The Verge",
            "https://www.wired.com/feed/rss": "WIRED",
        },
    )
    return run_dir, product_id, run_id


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )


def test_per_source_counts_expands_rss_streams(temp_run):
    run_dir, product_id, run_id = temp_run
    _write_jsonl(run_dir / "fetch.jsonl", [
        {"source": "rss", "file_name": "rss-acme.jsonl", "line_count": 10},
        {"source": "rss", "file_name": "rss-acme-2.jsonl", "line_count": 7},
        {"source": "rss", "file_name": "rss-acme-3.jsonl", "line_count": 5},
        {"source": "reddit_rss", "file_name": "reddit_rss-acme.jsonl",
         "line_count": 20},
        {"source": "reddit_rss", "file_name": "reddit_rss-acme-2.jsonl",
         "line_count": 3},
    ])
    _write_jsonl(run_dir / "normalize.jsonl", [
        {"source": "rss", "source_display_name": "rss-acme",
         "filter_status": "passed", "is_relevant": True},
        {"source": "rss", "source_display_name": "rss-acme",
         "filter_status": "passed", "is_relevant": True},
        {"source": "rss", "source_display_name": "rss-acme-2",
         "filter_status": "passed", "is_relevant": True},
        {"source": "rss", "source_display_name": "reddit_rss-acme",
         "filter_status": "passed", "is_relevant": True},
    ])

    from webui.app import _per_source_counts
    result = _per_source_counts(
        product_id, run_id,
        [{"stage": "fetch"}, {"stage": "normalize"}],
    )

    labels = {sid: disp for sid, disp in result["sources"]}
    assert "Media Coverage Sources" not in labels.values()
    assert labels["rss-acme"] == "TechCrunch"
    assert labels["rss-acme-2"] == "The Verge"
    assert labels["rss-acme-3"] == "WIRED"
    assert labels["reddit_rss-acme"] == "r/supabase"
    assert labels["reddit_rss-acme-2"] == "r/webdev"

    assert result["by_stage"]["fetch"]["rss-acme"] == 10
    assert result["by_stage"]["fetch"]["rss-acme-2"] == 7
    assert result["by_stage"]["normalize"]["rss-acme"] == 2
    assert result["by_stage"]["normalize"]["rss-acme-2"] == 1
    # Aggregated plugin key must not appear.
    assert "rss" not in result["by_stage"]["fetch"]
    assert "rss" not in labels


def test_per_source_counts_source_id_filter_keeps_streams(temp_run):
    """--source-ids=rss must keep stream rows whose source id is rss."""
    run_dir, product_id, run_id = temp_run
    _write_jsonl(run_dir / "fetch.jsonl", [
        {"source": "rss", "file_name": "rss-acme.jsonl", "line_count": 4},
        {"source": "reddit_rss", "file_name": "reddit_rss-acme.jsonl",
         "line_count": 9},
    ])
    snap = run_dir / "config_snapshot"
    snap.mkdir()
    (snap / "runtime.json").write_text(json.dumps({
        "runtime_context": {"cli_args": {"source_ids": "rss"}},
    }), encoding="utf-8")

    from webui.app import _per_source_counts
    result = _per_source_counts(
        product_id, run_id, [{"stage": "fetch"}],
    )
    ids = {sid for sid, _ in result["sources"]}
    assert ids == {"rss-acme"}
