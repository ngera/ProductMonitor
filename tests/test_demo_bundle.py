"""The offline demo bundle is complete and matches the current prompts.

`product-monitor demo` is the README's first instruction. It failed on every
fresh clone because products/demo/ and data/demo/ were not in git, and it
fails with a replay miss whenever a relevance or classify prompt changes
without the bundle being rebuilt (`python scripts/build_demo_replay.py`).
"""

from __future__ import annotations

import json

import pytest

from pipeline import demo


def test_bundle_files_present():
    assert demo._bundle_check() is None


def test_every_demo_prompt_has_a_replay_answer():
    from pipeline import classify, relevance
    from pipeline.config import set_current_product
    from pipeline.extract import extract
    from pipeline.llm import _prompt_key
    from pipeline.product import load_product

    load_product.cache_clear()
    product = load_product("demo")
    set_current_product(product)

    replay = {
        json.loads(line)["key"]
        for line in demo.DEMO_REPLAY_JSONL.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    raw = [
        json.loads(line)
        for line in demo.DEMO_RAW_JSONL.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(raw) == 15

    missing = []
    for rec in raw:
        if not relevance._passes_pre_gate(rec, product):
            missing.append(("pre-gate", rec["title"]))
            continue
        system, user = relevance._render_prompt(rec["title"], rec["body"])
        if _prompt_key("relevance", system, user) not in replay:
            missing.append(("relevance", rec["title"]))
        row = {
            "id": f"hn:{rec['external_id']}",
            "title": rec["title"],
            "body": rec["body"],
            "source": "hn",
            "source_display_name": "Hacker News",
            "engagement_json": json.dumps(rec.get("engagement", {})),
            "raw": {},
        }
        system, user = classify._build_prompt(row, extract(f"{rec['title']}\n{rec['body']}"))
        if _prompt_key("classify", system, user) not in replay:
            missing.append(("classify", rec["title"]))

    if missing:
        pytest.fail(
            "demo bundle is stale — run `python scripts/build_demo_replay.py`: "
            + "; ".join(f"{stage}: {title}" for stage, title in missing)
        )
