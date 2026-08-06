"""Curated media coverage sources catalog.

Backs the "Media coverage sources" section on Admin > Connections. Not a
fetch driver — these are just RSS feed URLs the user can copy into any
product's `sources.yaml` under `type: rss`. See report_v2_design.md §4.8
for how items from these feeds land in the digest's Media Coverage section
(via the classifier's `news_discussion` content type, not via source type).

Loaded from `config/media_sources.yaml`. When the file is missing or
malformed, falls back to a minimal hardcoded list so the Connections page
still renders sensibly on a fresh install.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from pipeline.config import CONFIG_DIR


_YAML_PATH = CONFIG_DIR / "media_sources.yaml"


# Fallback list used when the YAML file is missing/malformed. Keep small —
# users should edit the YAML for the full catalog.
_FALLBACK: list[dict[str, Any]] = [
    {"name": "Wired", "domain": "wired.com",
     "feed_url": "https://www.wired.com/feed/rss",
     "content_types": ["media_coverage"]},
    {"name": "The Verge", "domain": "theverge.com",
     "feed_url": "https://www.theverge.com/rss/index.xml",
     "content_types": ["media_coverage"]},
    {"name": "Ars Technica", "domain": "arstechnica.com",
     "feed_url": "https://feeds.arstechnica.com/arstechnica/index",
     "content_types": ["media_coverage"]},
    {"name": "TechCrunch", "domain": "techcrunch.com",
     "feed_url": "https://techcrunch.com/feed/",
     "content_types": ["media_coverage"]},
]


@dataclass(frozen=True)
class MediaSource:
    name: str
    domain: str
    feed_url: str


# ADR-0021: every catalog entry can carry a `content_types` list. Absent →
# defaults to ["media_coverage"] since the catalog was originally scoped to
# publications only. Operators wanting to expose a feed as user_feedback
# (rare — RSS is usually one-way news) declare that explicitly per-entry.
_DEFAULT_CONTENT_TYPES = ["media_coverage"]
_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset({"user_feedback", "media_coverage"})


def _sanitize(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Drop malformed entries silently (missing name / feed_url)."""
    name = str(entry.get("name") or "").strip()
    feed_url = str(entry.get("feed_url") or "").strip()
    if not name or not feed_url:
        return None
    raw_types = entry.get("content_types") or _DEFAULT_CONTENT_TYPES
    if not isinstance(raw_types, list):
        raw_types = _DEFAULT_CONTENT_TYPES
    content_types = [t for t in raw_types if t in _ALLOWED_CONTENT_TYPES]
    if not content_types:
        content_types = list(_DEFAULT_CONTENT_TYPES)
    return {
        "name": name,
        "domain": str(entry.get("domain") or "").strip(),
        "feed_url": feed_url,
        "content_types": content_types,
    }


def load() -> list[dict[str, Any]]:
    """Return the curated media source list. Sorted by name (case-insensitive).

    Reads `config/media_sources.yaml` on every call — cheap, and lets the
    admin edit the file without restarting the server. Falls back to
    `_FALLBACK` if the file is missing or unreadable.
    """
    if not _YAML_PATH.exists():
        return sorted(_FALLBACK, key=lambda s: s["name"].lower())
    try:
        raw = yaml.safe_load(_YAML_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return sorted(_FALLBACK, key=lambda s: s["name"].lower())
    entries = raw.get("media_sources") or []
    cleaned = [e for e in (_sanitize(x) for x in entries if isinstance(x, dict)) if e]
    if not cleaned:
        return sorted(_FALLBACK, key=lambda s: s["name"].lower())
    return sorted(cleaned, key=lambda s: s["name"].lower())


# ---------------------------------------------------------------------------
# Per-product enable helper (Option A of the "does media coverage auto-fetch?"
# UX question — no, but this is the one-click way to make it fetch for a
# given product). Appends every catalog entry to products/<id>/sources.yaml
# as a `type: rss` source; deduplicates by feed URL so it's idempotent.
# ---------------------------------------------------------------------------


import re


def _slug(name: str) -> str:
    """Simple stable slug for source ids — lowercase, non-alnum → dashes."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s or "media"


def _sources_yaml_path(product_id: str):
    from pipeline.config import project_root
    return project_root() / "products" / product_id / "sources.yaml"


def _read_sources_yaml(product_id: str) -> dict:
    path = _sources_yaml_path(product_id)
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def status_for_product(product_id: str) -> dict:
    """Return {'total': N, 'enabled': M, 'missing_names': [...]} for the product.

    A media source counts as "enabled" if the product's sources.yaml has any
    `type: rss` entry with a matching `url`. That's the URL-based identity —
    doesn't matter what `id` or `display` the entry uses.
    """
    catalog = load()
    data = _read_sources_yaml(product_id)
    present_urls: set[str] = set()
    for s in (data.get("sources") or []):
        if isinstance(s, dict) and s.get("type") == "rss":
            url = s.get("url")
            if isinstance(url, str) and url:
                present_urls.add(url)
    enabled = 0
    missing_names: list[str] = []
    for m in catalog:
        if m["feed_url"] in present_urls:
            enabled += 1
        else:
            missing_names.append(m["name"])
    return {"total": len(catalog), "enabled": enabled, "missing_names": missing_names}


def catalog_cards(product) -> list[dict[str, Any]]:
    """Turn every media_sources.yaml entry into a UI card record.

    Each catalog entry is a specific feed URL that maps 1:1 to a stream on the
    product's `rss` source (if enabled). The card carries enough state for
    the Edit Sources page to render enable/disable + pause + display name
    without any per-stream config surface (feed_url is fixed by the catalog).

    Returned card shape:
      {
        "kind": "catalog_entry",
        "plugin_id": "rss",           # backing plugin — always rss for now
        "catalog_name": "The Verge",  # the identity users recognize
        "display_name": "The Verge",
        "domain": "theverge.com",
        "feed_url": "https://.../rss",
        "content_types": ["media_coverage"],
        "source_category": "rss_feed",
        "enabled": bool,              # is there an rss stream on the product with this feed_url?
        "stream_paused": bool,        # if enabled, is that stream paused?
        "instance_paused": bool,      # rss source-level pause on the product
        "instance_id": "rss",         # for saves back to sources.yaml
      }
    """
    catalog = load()
    # Build a feed_url → (stream_paused, instance_paused) lookup from the
    # product's existing rss source instance. product.sources are plain dicts.
    rss_streams: dict[str, dict[str, bool]] = {}
    rss_instance_paused = False
    for src in (getattr(product, "sources", None) or []):
        if not isinstance(src, dict) or src.get("type") != "rss":
            continue
        rss_instance_paused = bool(src.get("paused")) or rss_instance_paused
        for stream in (src.get("streams") or []):
            fu = stream.get("feed_url")
            if fu:
                rss_streams[fu] = {"paused": bool(stream.get("paused"))}
    out: list[dict[str, Any]] = []
    for entry in catalog:
        fu = entry["feed_url"]
        stream_state = rss_streams.get(fu)
        out.append({
            "kind": "catalog_entry",
            "plugin_id": "rss",
            "catalog_name": entry["name"],
            "display_name": entry["name"],
            "domain": entry.get("domain") or "",
            "feed_url": fu,
            "content_types": list(entry.get("content_types") or ["media_coverage"]),
            "source_category": "rss_feed",
            "enabled": stream_state is not None,
            "stream_paused": bool(stream_state and stream_state.get("paused")),
            "instance_paused": rss_instance_paused,
            "instance_id": "rss",
        })
    return out


def enable_all_for_product(product_id: str) -> dict:
    """Append every missing catalog entry to products/<id>/sources.yaml.

    Returns {'added': [names], 'already_present': [names]}. Idempotent:
    calling twice in a row adds nothing the second time.
    """
    catalog = load()
    path = _sources_yaml_path(product_id)
    data = _read_sources_yaml(product_id)
    sources_list = list(data.get("sources") or [])

    existing_urls: set[str] = {
        s.get("url") for s in sources_list
        if isinstance(s, dict) and s.get("type") == "rss" and s.get("url")
    }
    existing_ids: set[str] = {
        s.get("id") for s in sources_list
        if isinstance(s, dict) and s.get("id")
    }

    added: list[str] = []
    already_present: list[str] = []
    for m in catalog:
        if m["feed_url"] in existing_urls:
            already_present.append(m["name"])
            continue

        base = "media-" + _slug(m["name"])
        source_id = base
        n = 2
        while source_id in existing_ids:
            source_id = f"{base}-{n}"
            n += 1
        existing_ids.add(source_id)

        sources_list.append({
            "type": "rss",
            "id": source_id,
            "url": m["feed_url"],
            "display": f"Media · {m['name']}",
        })
        added.append(m["name"])

    if added:
        data["sources"] = sources_list
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True,
                           default_flow_style=False),
            encoding="utf-8",
        )
        # Invalidate the product cache so the next load_product() sees the
        # new sources without a webui restart.
        try:
            from pipeline import product as _p
            _p.clear_cache()
        except Exception:
            pass

    return {"added": added, "already_present": already_present}
