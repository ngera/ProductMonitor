"""An empty "relevant" classification is a failure, not a success.

Regression for the Supabase 2026-W39 digest: llama3 answered a Verge article
about Destiny 2 with `is_topic_relevant: true` and nothing else. The schema
only requires that one field, so the answer validated, the item was marked
classified, and it reached the press block with an empty summary.
"""

from __future__ import annotations

from types import SimpleNamespace

from pipeline import classify
from pipeline.models import CoreClassification, is_degenerate_classification


def test_degenerate_detection():
    assert is_degenerate_classification(CoreClassification(is_topic_relevant=True))
    assert not is_degenerate_classification(
        CoreClassification(is_topic_relevant=True, content_types=["praise"])
    )
    assert not is_degenerate_classification(
        CoreClassification(is_topic_relevant=True, summary="Users like the editor.")
    )
    # An explicit "not relevant" with no labels is a real answer.
    assert not is_degenerate_classification(CoreClassification(is_topic_relevant=False))


class _Client:
    model = "llama3:latest"

    def __init__(self, answers):
        self._answers = answers

    def structured(self, system, prompt, schema):
        return self._answers[prompt]


def test_run_classify_marks_degenerate_answer_failed(monkeypatch):
    items = [
        {"id": "destiny", "title": "Bungie is not done with Destiny", "body": "",
         "relevance_score": 0.0, "source": "rss"},
        {"id": "real", "title": "Supabase auth broke", "body": "",
         "relevance_score": 0.9, "source": "reddit_rss"},
    ]
    statuses: list[tuple[str, str]] = []
    persisted: list[str] = []
    failures: list[str] = []
    queries: list[tuple[str, list]] = []

    def fake_query(sql, params=None):
        queries.append((sql, params))
        return items if "SELECT i.*" in sql else [{"n": 0}]

    monkeypatch.setattr(classify, "app_config", lambda: {"classify": {"max_attempts": 2}})
    monkeypatch.setattr(
        classify, "current_product",
        lambda: SimpleNamespace(classification_schema=CoreClassification),
    )
    monkeypatch.setattr(classify.storage, "query", fake_query)
    monkeypatch.setattr(classify.storage, "add_classify_failures", failures.extend)
    monkeypatch.setattr(classify.storage, "set_relevance_batch", lambda rows: None)
    monkeypatch.setattr(classify.storage, "set_filter_status_batch", statuses.extend)
    monkeypatch.setattr(classify, "extract", lambda text: None)
    monkeypatch.setattr(classify, "_build_prompt", lambda it, rx: ("sys", it["id"]))
    monkeypatch.setattr(classify, "choose_primary_area", lambda *a: "auth")
    monkeypatch.setattr(classify, "_stage_persist",
                        lambda batch, item_id, *a: persisted.append(item_id))
    monkeypatch.setattr(classify, "_flush_persist", lambda batch: None)

    client = _Client({
        "destiny": CoreClassification(is_topic_relevant=True),
        "real": CoreClassification(is_topic_relevant=True,
                                   content_types=["bug_report"],
                                   summary="Auth fails after upgrade."),
    })
    out = classify.run_classify("2026-W39", client=client)

    assert out["counters"]["degenerate"] == 1
    assert out["counters"]["failed"] == 1
    assert out["counters"]["classified"] == 1
    assert ("destiny", "classification_failed") in statuses
    assert persisted == ["real"]
    # The failure is counted, and items at the cap are not selected again.
    assert failures == ["destiny"]
    select_sql, select_params = next(q for q in queries if "SELECT i.*" in q[0])
    assert "classify_attempts" in select_sql and 2 in select_params
