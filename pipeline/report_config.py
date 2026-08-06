"""Digest v2 per-product config loader — products/<id>/report_config.yaml.

Merges app.yaml `digest:` defaults with per-product overrides. Missing file
means all defaults, which is fine — the wizard writes the file explicitly
in Slice 4. Values are cheap to compute so no caching layer.

See [report_v2_design.md §7.1](../documents/report_v2_design.md).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pipeline.config import app_config, project_root


# Section defaults — sections toggle on unless explicitly disabled.
_DEFAULT_SECTIONS = {
    "positive": True,
    "negative": True,
    "bugs": True,
    "features": True,
    "competition": False,  # opt-in via wizard
}


def path_for(product_id: str) -> Path:
    return project_root() / "products" / product_id / "report_config.yaml"


def load(product_id: str) -> dict[str, Any]:
    """Effective digest config — per-product overrides merged onto app.yaml defaults."""
    app_digest = app_config().get("digest", {}) or {}
    app_thresholds = app_digest.get("sentiment_thresholds") or {}

    cfg: dict[str, Any] = {
        "sections": dict(_DEFAULT_SECTIONS),
        "sentiment_thresholds": {
            "positive": float(app_thresholds.get("positive", 0.2)),
            "negative": float(app_thresholds.get("negative", -0.2)),
        },
        "headline_top_n": int(app_digest.get("headline_top_n", 25)),
        "trend_buckets": int(app_digest.get("trend_buckets", 12)),
        # Prefer the calendar-month key; fall back to the legacy day-count
        # key so existing app.yaml overrides keep working during migration.
        "trend_bucket_switch_months": int(
            app_digest.get("trend_bucket_switch_months")
            or max(1, int(app_digest.get("trend_bucket_switch_days") or 365) // 30)
        ),
    }

    p = path_for(product_id)
    if not p.exists():
        return cfg
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return cfg
    over = raw.get("digest_v2") or {}

    if isinstance(over.get("sections"), dict):
        for k, v in over["sections"].items():
            if k in cfg["sections"]:
                cfg["sections"][k] = bool(v)

    if isinstance(over.get("sentiment_thresholds"), dict):
        for k in ("positive", "negative"):
            if k in over["sentiment_thresholds"]:
                cfg["sentiment_thresholds"][k] = float(over["sentiment_thresholds"][k])

    if over.get("headline_top_n") is not None:
        cfg["headline_top_n"] = int(over["headline_top_n"])

    return cfg
