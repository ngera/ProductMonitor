"""Post-wizard 'Calibrate more' service.

Reuses `pipeline.minifetch` against a LIVE product's sources so operators can
grow their labeled-snippet pool after the initial wizard run. Judgments are
buffered in `data/.wizard/product-<id>/judgments.json`, then flushed to
`products/<id>/examples/{positive,negative}/*.yaml` on 'Done'.

Namespace note: slug is `product-<product_id>` (with the `product-` prefix)
so the on-disk state can never collide with a wizard-draft slug.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from pipeline import minifetch as _mf


def slug_for(product_id: str) -> str:
    return f"product-{product_id}"


def _dir_for(product_id: str) -> Path:
    return _mf._draft_dir(slug_for(product_id))


def judgments_path(product_id: str) -> Path:
    return _dir_for(product_id) / "judgments.json"


def read_judgments(product_id: str) -> dict[str, dict[str, Any]]:
    p = judgments_path(product_id)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _write_judgments(product_id: str, data: dict[str, dict[str, Any]]) -> None:
    d = _dir_for(product_id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = judgments_path(product_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(judgments_path(product_id))


def record_judgment(
    product_id: str, item_id: str, verdict: str, corpus_item: dict[str, Any],
) -> None:
    """Record one calibrate judgment. Skips 'skip' verdicts (dropped items
    aren't materialized). Overwrite semantics: same item_id replaces prior."""
    if verdict not in ("relevant", "not_relevant"):
        return
    polarity = "positive_example" if verdict == "relevant" else "negative_example"
    data = read_judgments(product_id)
    data[item_id] = {
        "item_id": item_id,
        "polarity": polarity,
        "title": corpus_item.get("title") or "",
        "body": corpus_item.get("body") or "",
        "source_url": corpus_item.get("url") or "",
        "judged_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_judgments(product_id, data)


def reset(product_id: str) -> None:
    """Discard minifetch corpus + judgments so the user can start over."""
    _mf.discard_corpus(slug_for(product_id))


def product_to_suggested_sources(
    product, selected_source_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Translate `product.sources` into the shape `pipeline.minifetch` expects.

    One suggested_sources entry per (source instance); its first stream becomes
    the base stream_config, remaining streams go into `_extra_streams` for the
    minifetch loop to expand.

    `selected_source_ids` optionally filters which source instances to include;
    an empty list means 'all non-paused'. `product.sources` and each stream
    are plain dicts (pipeline.product.ProductSpec).
    """
    out: list[dict[str, Any]] = []
    selected = set(selected_source_ids or [])
    for src in (product.sources or []):
        if src.get("paused"):
            continue
        src_id = src.get("id")
        if selected and src_id not in selected:
            continue
        streams = src.get("streams") or []
        if not streams:
            continue
        def _to_dict(s):
            return {k: v for k, v in s.items() if k != "paused"}

        base = _to_dict(streams[0])
        extras = [_to_dict(s) for s in streams[1:] if not s.get("paused")]
        if extras:
            base["_extra_streams"] = extras
        out.append({
            "plugin_id": src.get("type"),
            "stream_config": base,
            "enabled": True,
            "requires_key": False,
            "rationale": "",
        })
    return out


def product_to_facts(product) -> dict[str, Any]:
    """Pull the fields `minifetch` uses for keyword gating."""
    return {
        "display": product.display or product.id,
        "aliases": list(product.aliases or []),
        "scope_in": list(product.scope_in or []),
        "scope_out": list(product.scope_out or []),
        "description": product.description or "",
    }


_SLUG_RE = __import__("re").compile(r"[^a-z0-9]+")


def _snippet_slug(title: str, item_id: str) -> str:
    slug = _SLUG_RE.sub("-", (title or "").lower()).strip("-")[:60]
    if not slug:
        slug = (item_id or "post").replace(":", "-")[:60]
    return slug


def flush_to_snippets(product_id: str, product_dir: Path) -> tuple[int, int]:
    """Write buffered judgments as snippet YAML files under
    products/<id>/examples/{positive,negative}/. Returns (n_positive, n_negative).

    Duplicate slugs get numeric suffixes. Judgments dict is cleared and the
    minifetch corpus discarded on success — the calibrate landing page then
    lands back at 'not_started' so a subsequent visit starts fresh.
    """
    judgments = read_judgments(product_id)
    if not judgments:
        return (0, 0)
    ex_pos = product_dir / "examples" / "positive"
    ex_neg = product_dir / "examples" / "negative"
    ex_pos.mkdir(parents=True, exist_ok=True)
    ex_neg.mkdir(parents=True, exist_ok=True)
    n_pos = n_neg = 0
    for _item_id, entry in judgments.items():
        polarity = entry.get("polarity")
        if polarity == "positive_example":
            target = ex_pos
            n_pos += 1
        elif polarity == "negative_example":
            target = ex_neg
            n_neg += 1
        else:
            continue
        slug = _snippet_slug(entry.get("title") or "", entry.get("item_id") or "")
        blob = {
            "source_url": entry.get("source_url") or None,
            "title": entry.get("title") or None,
            "body": entry.get("body") or "",
            "polarity": polarity,
            "holdout_eval": False,
            "labels": {"is_topic_relevant": polarity == "positive_example"},
            "notes": f"seed from calibrate-more @ {entry.get('judged_at', '')}",
            "created_at": entry.get("judged_at") or "",
        }
        out = target / f"{slug}.yaml"
        i = 1
        while out.exists():
            out = target / f"{slug}-{i}.yaml"
            i += 1
        out.write_text(
            yaml.safe_dump(blob, sort_keys=False, allow_unicode=True,
                           default_flow_style=False),
            encoding="utf-8",
        )
    reset(product_id)
    return (n_pos, n_neg)


def sample_deck(product_id: str, size: int = 10) -> list[dict[str, Any]]:
    """Return the next batch of unjudged items from the minifetch corpus."""
    judged = set(read_judgments(product_id).keys())
    return _mf.sample_deck(
        slug_for(product_id), size=size, exclude_ids=judged,
    )


def read_status(product_id: str):
    return _mf.read_status(slug_for(product_id))


def start(
    product,
    selected_source_ids: list[str],
    *,
    use_llm_gate: bool = False,
) -> None:
    """Kick off a mini-fetch against the selected product sources."""
    _mf.start_minifetch(
        slug_for(product.id),
        product_to_suggested_sources(product, selected_source_ids),
        product_facts=product_to_facts(product),
        use_llm_gate=use_llm_gate,
    )


def find_corpus_item(product_id: str, item_id: str) -> dict[str, Any] | None:
    """Look up an item in the minifetch corpus by id (used when recording a
    judgment to pull title/body/url for the snippet YAML)."""
    for item in _mf.load_corpus(slug_for(product_id)):
        if item.get("id") == item_id:
            return item
    return None
