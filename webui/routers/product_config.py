"""Per-product configuration: sources, prompts, report, taxonomy, YAML editor.

Split out of webui/app.py (ADR-0026). Shared helpers live in
webui/core.py and are reached as `core.<name>` so a test patch there
reaches every caller.
"""

from __future__ import annotations

import json as _json
import yaml
from datetime import date
from fastapi import APIRouter, Body, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pathlib import Path
from pipeline.product import load_product
from typing import Any, Optional

from webui import core

router = APIRouter()


def _build_source_cards(product, globally_paused: set[str]) -> list[dict]:
    """ADR-0021 taxonomy view: plugin cards + media catalog cards, each
    tagged with source_category + content_types so the template can group
    them under User Feedback / Media Coverage sections.

    The `rss` plugin itself is intentionally EXCLUDED — its user-facing
    equivalent is the individual media catalog entries (each feed = one
    card). Custom RSS URLs not in the catalog can only be added via the
    raw YAML editor.

    Card shapes are distinguished by `kind`:
      - "plugin"         — a registered Source plugin with stream_fields
      - "catalog_entry"  — a single media_sources.yaml entry
    """
    from sources.registry import get_registry
    from pipeline import media_sources as _media
    import os
    from dotenv import dotenv_values
    env_snapshot = dict(os.environ)
    env_p = Path(__file__).resolve().parent.parent / ".env"
    if env_p.exists():
        try:
            for k, v in (dotenv_values(env_p) or {}).items():
                if v:
                    env_snapshot.setdefault(k, v)
        except Exception:
            pass

    def _ready(manifest) -> bool:
        # Only *required* connection fields block readiness. Optional
        # secrets (STACKEX_KEY, DISCOURSE_API_KEY, …) must not hide the
        # plugin from Add Source / wizard pick lists.
        req = [f for f in manifest.connection_fields
               if getattr(f, "required", False)]
        if not req:
            return True
        return all(env_snapshot.get(f.name, "").strip() for f in req)

    existing_by_type: dict[str, dict] = {
        s.get("type"): s for s in (product.sources or []) if s.get("type")
    }
    # Any rss stream on the product whose feed_url matches a catalog entry is
    # rendered as a catalog card, not under the rss plugin. Non-catalog rss
    # streams (custom URLs) surface only in the raw YAML editor.
    catalog_urls: set[str] = {e["feed_url"] for e in _media.load()}

    reg = get_registry()
    cards: list[dict] = []
    for plugin in reg.all_plugins():
        m = plugin.manifest
        if getattr(m, "category", "source") != "source":
            continue
        if m.plugin_id == "rss":
            continue   # excluded — see docstring
        if not _ready(m):
            continue
        instance = existing_by_type.get(m.plugin_id)
        conn_paused = m.plugin_id in globally_paused
        streams: list[dict] = []
        if instance:
            for si, stream in enumerate(instance.get("streams") or []):
                streams.append({
                    "index": si,
                    "data": stream,
                    "identifier": core._stream_identifier(m.plugin_id, stream),
                    "display": stream.get("display") or stream.get("name") or "",
                    "paused": bool(stream.get("paused")),
                    "instance_paused": bool(instance.get("paused")),
                    "connection_paused": conn_paused,
                })
        stream_fields = [
            {
                "name": f.name, "label": f.label, "type": f.type,
                "required": f.required,
                "default": f.default if f.default is not None else "",
                "placeholder": f.placeholder or "", "help": f.help,
            }
            for f in m.stream_fields if f.name != "name"
        ]
        cards.append({
            "kind": "plugin",
            "plugin_id": m.plugin_id,
            "display_name": m.display_name,
            "help": m.help,
            "source_category": getattr(m, "source_category", "custom_source"),
            "content_types": list(getattr(m, "content_types", ["user_feedback"])),
            "configured": instance is not None,
            "connection_paused": conn_paused,
            "instance": instance or {
                "id": m.plugin_id, "paused": False,
                "credibility_weight": m.credibility_weight_default,
            },
            "streams": streams,
            "stream_fields": stream_fields,
            "identifier_field": m.identifier_field,
            "supports_bulk_add": bool(getattr(m, "supports_bulk_add", False)),
        })

    # Add one card per media catalog entry.
    rss_conn_paused = "rss" in globally_paused
    for entry_card in _media.catalog_cards(product):
        cards.append({
            "kind": "catalog_entry",
            "plugin_id": "rss",
            "display_name": entry_card["display_name"],
            "help": entry_card.get("domain") or "",
            "source_category": entry_card["source_category"],
            "content_types": entry_card["content_types"],
            "configured": entry_card["enabled"],
            "connection_paused": rss_conn_paused,
            "feed_url": entry_card["feed_url"],
            "instance": {
                "id": entry_card["instance_id"], "paused": entry_card["instance_paused"],
                "credibility_weight": 1.0,
            },
            # Catalog cards don't have per-stream config surface — the whole
            # card IS one stream (identity = feed_url). We still expose its
            # pause state via a single virtual stream row.
            "stream_paused": entry_card["stream_paused"],
        })
    return cards


def _group_cards_by_taxonomy(cards: list[dict]) -> list[dict]:
    """Group cards into [content_type][source_category] sections.

    Dual-tagged plugins (e.g. Product Hunt) appear once under their
    *primary* content_type — the first of user_feedback / media_coverage
    listed on the card. Chips on the card still show every content type
    (ADR-0031; supersedes the dual-list UI rule in ADR-0021).
    """
    top_order = ["user_feedback", "media_coverage"]
    sub_order = ["rss_feed", "custom_source", "third_party_scraper"]
    top_titles = {
        "user_feedback": "User Feedback Sources",
        "media_coverage": "Media Coverage Sources",
    }
    sub_titles = {
        "rss_feed": "RSS Feeds",
        "custom_source": "Custom Sources",
        "third_party_scraper": "Third-party Scrapers",
    }

    def _primary_ct(card: dict) -> str:
        cts = card.get("content_types") or []
        for ct in top_order:
            if ct in cts:
                return ct
        return cts[0] if cts else "user_feedback"

    sections: list[dict] = []
    for ct in top_order:
        subs: list[dict] = []
        for sc in sub_order:
            matching = [c for c in cards
                        if _primary_ct(c) == ct
                        and c.get("source_category") == sc]
            if not matching:
                continue
            # Bundle all catalog cards into a single "master panel" so the UI
            # can show them as one grouped chooser (Select all / Select none)
            # instead of 16 individual cards that require per-site clicks.
            plugin_cards = [c for c in matching if c.get("kind") != "catalog_entry"]
            catalog_cards = [c for c in matching if c.get("kind") == "catalog_entry"]
            plugin_cards.sort(key=lambda c: (0 if c.get("configured") else 1,
                                              c.get("display_name", "").lower()))
            catalog_cards.sort(key=lambda c: c.get("display_name", "").lower())
            subs.append({
                "source_category": sc,
                "title": sub_titles[sc],
                "cards": plugin_cards,
                "catalog_cards": catalog_cards,
                "catalog_enabled_count": sum(1 for c in catalog_cards if c.get("configured")),
                "catalog_total": len(catalog_cards),
            })
        if subs:
            sections.append({
                "content_type": ct,
                "title": top_titles[ct],
                "subsections": subs,
            })
    return sections


def _configured_summary(cards: list[dict]) -> list[dict]:
    """Compact table rows for the top 'Configured sources' section.

    One row per configured entity:
      - Each configured plugin card → one row (stream_count = # of streams)
      - All catalog entries collapse into a single 'Media Coverage Sources'
        row when at least one is enabled (stream_count = 'N of M enabled')

    Non-configured plugins are hidden — they live behind '+ Add more sources'.
    """
    rows: list[dict] = []
    catalog_enabled = 0
    catalog_total = 0
    catalog_paused = False
    catalog_conn_paused = False
    for c in cards:
        if c.get("kind") == "catalog_entry":
            catalog_total += 1
            if c.get("configured"):
                catalog_enabled += 1
            catalog_paused = catalog_paused or bool((c.get("instance") or {}).get("paused"))
            catalog_conn_paused = catalog_conn_paused or bool(c.get("connection_paused"))
            continue
        if not c.get("configured"):
            continue
        instance = c.get("instance") or {}
        rows.append({
            "kind": "plugin",
            "plugin_id": c["plugin_id"],
            "display_name": c["display_name"],
            "content_types": list(c.get("content_types") or []),
            "source_category": c.get("source_category", "custom_source"),
            "stream_count": len(c.get("streams") or []),
            "stream_count_label": str(len(c.get("streams") or [])),
            "instance_paused": bool(instance.get("paused")),
            "connection_paused": bool(c.get("connection_paused")),
        })
    if catalog_enabled > 0:
        rows.append({
            "kind": "catalog",
            "plugin_id": "rss",
            "display_name": "Media Coverage Sources",
            "content_types": ["media_coverage"],
            "source_category": "rss_feed",
            "stream_count": catalog_enabled,
            "stream_count_label": f"{catalog_enabled} of {catalog_total} enabled",
            "instance_paused": catalog_paused,
            "connection_paused": catalog_conn_paused,
        })
    rows.sort(key=lambda r: r["display_name"].lower())
    return rows


@router.get("/products/{product_id}/sources", response_class=HTMLResponse)
def sources_form(request: Request, product_id: str,
                  media_enabled: Optional[str] = None,
                  discover_note: Optional[str] = None):
    product = core._product_or_404(product_id)
    from pipeline import connections as _conn
    from pipeline import media_sources as _media

    globally_paused = _conn.paused_types()
    cards = _build_source_cards(product, globally_paused)
    sections = _group_cards_by_taxonomy(cards)
    configured_summary = _configured_summary(cards)
    return core.templates.TemplateResponse(
        "sources_form.html",
        {
            "request": request,
            "product": product,
            "cards": cards,       # flat list retained for legacy references
            "sections": sections,
            "configured_summary": configured_summary,
            "media_status": _media.status_for_product(product_id),
            "media_flash": media_enabled,
            "discover_note": discover_note,
        },
    )


# ADR-0030 — stream auto-discovery. Two routes per (product, plugin):
#   GET  /products/{pid}/sources/{plugin_id}/discover — render candidates
#   POST /products/{pid}/sources/{plugin_id}/discover — approve the checked
#                                                       ones + append streams


@router.get("/products/{product_id}/sources/{plugin_id}/discover",
         response_class=HTMLResponse)
def sources_discover(request: Request, product_id: str, plugin_id: str):
    """Run the plugin's discover_streams() against the product profile
    and render the candidate list. GET is safe to reload; discovery is
    deterministic per (profile, plugin_id) because query generation is
    cached (see pipeline/stream_query_generation.py).
    """
    product = core._product_or_404(product_id)
    from sources.registry import get_registry
    reg = get_registry()
    plugin = reg.get(plugin_id) if reg else None
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"plugin {plugin_id!r} not found")

    # Build profile_facts from the product's YAML — same shape ADR-0030
    # documents for the discover_streams contract.
    profile_facts = {
        "display": product.display,
        "description": getattr(product, "description", "") or "",
        "aliases": list(getattr(product, "aliases", []) or []),
        "scope_in": list(getattr(product, "scope_in", []) or []),
        "scope_out": list(getattr(product, "scope_out", []) or []),
    }

    error: Optional[str] = None
    candidates: list[Any] = []
    try:
        source = plugin.source_cls()
    except Exception as e:
        error = (
            f"Can't run discovery for {plugin.manifest.display_name}: {e}. "
            f"Check the connection page and that credentials are set."
        )
        source = None
    if source is not None:
        try:
            candidates = source.discover_streams(profile_facts)
        except Exception as e:
            error = f"discover_streams failed: {e}"
            candidates = []

    # Filter out candidates that duplicate what's already in sources.yaml.
    existing_ids: set[str] = set()
    for src in product.sources:
        if src.get("type") != plugin_id:
            continue
        for stream in (src.get("streams") or []):
            # Use the plugin's identifier_field to compute a stable key
            # per stream — fall back to str(dict) if the manifest doesn't
            # declare one.
            id_field = getattr(plugin.manifest, "identifier_field", "") or ""
            if id_field and stream.get(id_field):
                existing_ids.add(str(stream[id_field]).lower())
    new_candidates = [
        c for c in candidates
        if not (
            (getattr(plugin.manifest, "identifier_field", "") or "")
            and str(c.stream_config.get(
                getattr(plugin.manifest, "identifier_field")
            ) or "").lower() in existing_ids
        )
    ]

    return core.templates.TemplateResponse(
        "sources_discover.html",
        {
            "request": request,
            "product": product,
            "plugin_id": plugin_id,
            "plugin_display": plugin.manifest.display_name,
            "candidates": new_candidates,
            "n_existing_hidden": len(candidates) - len(new_candidates),
            "error": error,
        },
    )


@router.post("/products/{product_id}/sources/{plugin_id}/discover")
async def sources_discover_save(
    product_id: str, plugin_id: str, request: Request,
):
    """Append checked candidates as new streams to the product's sources.yaml.

    Duplicates are guarded by the identifier_field: if a stream with the
    same identifier already exists on this source, the checked candidate
    is skipped silently.
    """
    product = core._product_or_404(product_id)
    from sources.registry import get_registry
    reg = get_registry()
    plugin = reg.get(plugin_id) if reg else None
    if plugin is None:
        raise HTTPException(status_code=404, detail=f"plugin {plugin_id!r} not found")

    form = await request.form()
    # The form encodes candidates as `candidate[<i>].<field>` bag —
    # simplest wire shape: each candidate ships as one hidden json blob
    # (`candidate_json`), checked ones also have `approve` in their name.
    approved_json = form.getlist("approve")
    approved: list[dict] = []
    for raw in approved_json:
        try:
            approved.append(_json.loads(raw))
        except Exception:
            continue

    if not approved:
        return RedirectResponse(
            url=f"/products/{product_id}/sources?discover_note=none+selected",
            status_code=303,
        )

    id_field = getattr(plugin.manifest, "identifier_field", "") or ""

    # Find or create the source entry for this plugin_id.
    sources = list(product.sources or [])
    src_entry: Optional[dict] = None
    for s in sources:
        if s.get("type") == plugin_id:
            src_entry = s
            break
    if src_entry is None:
        src_entry = {
            "id": plugin_id,
            "type": plugin_id,
            "credibility_weight": 1.0,
            "streams": [],
        }
        sources.append(src_entry)

    existing_streams = list(src_entry.get("streams") or [])
    existing_ids: set[str] = set()
    if id_field:
        for stream in existing_streams:
            v = stream.get(id_field)
            if v:
                existing_ids.add(str(v).lower())

    added: list[str] = []
    for cand in approved:
        stream_config = dict(cand.get("stream_config") or {})
        display = cand.get("display_name") or ""
        if id_field:
            key = str(stream_config.get(id_field) or "").lower()
            if not key or key in existing_ids:
                continue
            existing_ids.add(key)
        # Ensure the stream has a `name` so cursor logic distinguishes it.
        stream_config.setdefault(
            "name",
            f"{plugin_id}-{product_id}-{stream_config.get(id_field, len(existing_streams) + 1)}",
        )
        existing_streams.append(stream_config)
        added.append(display or stream_config.get(id_field) or "")

    src_entry["streams"] = existing_streams

    # Persist back to sources.yaml. Mirrors the safe-write pattern used by
    # the main sources_save handler above: backup, atomic replace, revert
    # on failure.
    product_dir = core._product_dir_for(product_id)
    sources_path = product_dir / "sources.yaml"
    backup_path = sources_path.with_suffix(".yaml.bak")
    if sources_path.exists():
        sources_path.replace(backup_path)
    try:
        sources_path.write_text(
            yaml.safe_dump(
                {"sources": sources},
                sort_keys=False, allow_unicode=True, default_flow_style=False,
            ),
            encoding="utf-8",
        )
        core.clear_cache()
        load_product(product_id)
    except Exception as e:
        if sources_path.exists():
            sources_path.unlink()
        if backup_path.exists():
            backup_path.replace(sources_path)
        core.clear_cache()
        raise HTTPException(status_code=422, detail={"errors": [str(e)]})
    if backup_path.exists():
        backup_path.unlink()

    if added:
        note = f"added+{len(added)}+stream(s):+" + ",+".join(a[:30] for a in added[:5])
    else:
        note = "no+new+streams+added"
    return RedirectResponse(
        url=f"/products/{product_id}/sources?discover_note={note}",
        status_code=303,
    )


@router.post("/products/{product_id}/media-coverage/enable-all")
def product_enable_media_coverage(product_id: str):
    """Append every missing media coverage feed URL to the product's
    sources.yaml as `type: rss` entries. Idempotent — already-present
    feeds (matched by URL) are skipped. See
    `pipeline/media_sources.py::enable_all_for_product` for details.

    This is the Option A path from the "does media coverage auto-fetch?"
    UX conversation — no auto-fetch, but one click on this button opts a
    product in.
    """
    core._product_or_404(product_id)
    from pipeline import media_sources as _media
    result = _media.enable_all_for_product(product_id)
    if result["added"]:
        note = f"added+{len(result['added'])}+feed(s)"
        if result["already_present"]:
            note += f"+({len(result['already_present'])}+already+present)"
    elif result["already_present"]:
        note = f"all+{len(result['already_present'])}+feeds+already+enabled"
    else:
        note = "no+media+feeds+in+catalog"
    return RedirectResponse(
        url=f"/products/{product_id}/sources?media_enabled={note}",
        status_code=303,
    )


@router.post("/products/{product_id}/sources")
def sources_save(product_id: str, payload: dict = Body(...)):
    product_dir = core._product_dir_for(product_id)
    sources_in = payload.get("sources") or []
    errors: list[str] = []
    cleaned: list[dict] = []
    seen_ids: set[str] = set()

    def _coerce(field: dict, raw: Any) -> Any:
        t = field["type"]
        if raw is None:
            raw = ""
        if t == "number":
            if isinstance(raw, str):
                raw = raw.strip()
            if raw == "" or raw is None:
                return field.get("default")
            try:
                v = float(raw)
                return int(v) if v.is_integer() else v
            except (TypeError, ValueError):
                return None
        if t == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in ("true", "1", "on", "yes")
        if t == "csv":
            if isinstance(raw, list):
                return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
            return [s.strip() for s in str(raw).split(",") if s.strip()]
        if t == "textarea_list":
            if isinstance(raw, list):
                return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
            return [s.strip() for s in str(raw).splitlines() if s.strip()]
        # text
        return str(raw).strip()

    for si, src in enumerate(sources_in):
        stype = (src.get("type") or "").strip()
        sid = (src.get("id") or "").strip().lower().replace(" ", "-")
        if not sid:
            errors.append(f"source #{si+1}: id is required")
            continue
        if sid in seen_ids:
            errors.append(f"source '{sid}' (#{si+1}): duplicate id")
            continue
        seen_ids.add(sid)
        if stype not in core.SOURCE_TYPE_META:
            errors.append(f"source '{sid}': unknown type {stype!r}")
            continue

        try:
            cred = float(src.get("credibility_weight", 1.0) or 1.0)
        except (TypeError, ValueError):
            errors.append(f"source '{sid}': credibility_weight must be a number")
            continue

        streams_in = src.get("streams") or []
        if not streams_in:
            errors.append(f"source '{sid}': at least one stream is required")
            continue

        fields = core.SOURCE_TYPE_META[stype]["stream_fields"]
        cleaned_streams: list[dict] = []
        for sti, stream in enumerate(streams_in):
            clean_stream: dict = {}
            # Per-stream pause is an explicit field, not one of the
            # type-specific stream_fields. Preserve it unconditionally.
            if bool(stream.get("paused")):
                clean_stream["paused"] = True
            for field in fields:
                value = _coerce(field, stream.get(field["name"]))
                if field.get("required") and not value and value != 0 and value is not False:
                    errors.append(
                        f"source '{sid}' stream #{sti+1}: '{field['label']}' is required"
                    )
                if value is None or value == "" or value == []:
                    continue
                clean_stream[field["name"]] = value
            # reddit_rss: normalize `subreddit` to a bare name (strip r/,
            # URLs, /new.rss, etc.) so the fetch code has a clean input and
            # sources.yaml on disk stays uniform.
            if stype == "reddit_rss" and "subreddit" in clean_stream:
                from sources.reddit_rss import normalize_subreddit
                raw_sub = clean_stream["subreddit"]
                bare = normalize_subreddit(raw_sub)
                if not bare:
                    errors.append(
                        f"source '{sid}' stream #{sti+1}: subreddit "
                        f"{raw_sub!r} isn't a valid subreddit — use r/<name>"
                    )
                else:
                    clean_stream["subreddit"] = bare
            cleaned_streams.append(clean_stream)

        cleaned.append({
            "id": sid,
            "type": stype,
            # Product-level pause. Runs skip this source instance until
            # unpaused. Superseded by the global pause on /connections.
            "paused": bool(src.get("paused")),
            "credibility_weight": cred,
            "streams": cleaned_streams,
        })

    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    sources_path = product_dir / "sources.yaml"
    backup_path = sources_path.with_suffix(".yaml.bak")
    if sources_path.exists():
        sources_path.replace(backup_path)
    try:
        sources_path.write_text(
            yaml.safe_dump({"sources": cleaned}, sort_keys=False, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        core.clear_cache()
        load_product(product_id)
    except Exception as e:
        if sources_path.exists():
            sources_path.unlink()
        if backup_path.exists():
            backup_path.replace(sources_path)
        core.clear_cache()
        raise HTTPException(status_code=422, detail={"errors": [str(e)]})
    if backup_path.exists():
        backup_path.unlink()
    return {"ok": True, "count": len(cleaned)}


# --- Prompts form (Phase 6) -------------------------------------------------
#
# Form-based editor for the relevance + classify LLM prompts. Each stage
# has its own group of fields: system message, user-prompt template, few-shot
# config, and (classify-only) extras instructions. The available template
# placeholders are listed on the right as a reference panel.

PROMPT_PLACEHOLDERS = {
    "relevance": [
        ("{product_display}", "Display name of the product (e.g., 'Microsoft Windows')."),
        ("{product_description}", "Description from product.yaml."),
        ("{title}", "Item's title (post title, issue title)."),
        ("{body}", "Item's body, truncated to 1000 chars."),
        ("{few_shot_block}", "Auto-rendered few-shot examples (when few_shot.enabled is true and the product has snippets)."),
    ],
    "classify": [
        ("{areas}", "Multi-line list of enabled areas (id: display) for the LLM to pick from."),
        ("{features}", "Hierarchical block: for each area, the features under it with each feature's description (the LLM-recognition prompt you wrote in the taxonomy editor). Use this to give the classifier the per-feature definitions you authored. Truncated to ~200 chars per feature so a 25-feature product stays under ~6KB."),
        ("{content_types}", "Comma-separated content-type vocabulary."),
        ("{extras_instructions}", "Free-form per-product notes (from the field below)."),
        ("{few_shot_block}", "Auto-rendered few-shot examples (when few_shot.enabled is true and the product has snippets)."),
        ("{build_numbers}", "Comma list of build numbers matched by regex."),
        ("{parent_block}", "For comments: the parent post title + body excerpt (auto-filled)."),
        ("{title}", "Item's title."),
        ("{body}", "Item's body, truncated to 4000 chars."),
        ("{engagement}", "Item engagement metrics JSON."),
        ("{source}", "Display name of the source instance."),
    ],
}


@router.get("/products/{product_id}/prompts", response_class=HTMLResponse)
def prompts_form(request: Request, product_id: str, saved: Optional[str] = None, error: Optional[str] = None):
    product = core._product_or_404(product_id)
    prompts = product.prompts or {}
    rel = prompts.get("relevance") or {}
    cls = prompts.get("classify") or {}
    return core.templates.TemplateResponse(
        "prompts_form.html",
        {
            "request": request,
            "product": product,
            "saved": saved,
            "error": error,
            "placeholders": PROMPT_PLACEHOLDERS,
            "values": {
                "relevance": {
                    "system": rel.get("system", "").rstrip("\n"),
                    "template": rel.get("template", "").rstrip("\n"),
                    "few_shot_enabled": bool((rel.get("few_shot") or {}).get("enabled", False)),
                    "few_shot_n_positive": int((rel.get("few_shot") or {}).get("n_positive", 3)),
                    "few_shot_n_negative": int((rel.get("few_shot") or {}).get("n_negative", 2)),
                },
                "classify": {
                    "system": cls.get("system", "").rstrip("\n"),
                    "template": cls.get("template", "").rstrip("\n"),
                    "extras_instructions": cls.get("extras_instructions", "").rstrip("\n"),
                    "few_shot_enabled": bool((cls.get("few_shot") or {}).get("enabled", False)),
                    "few_shot_n_positive": int((cls.get("few_shot") or {}).get("n_positive", 2)),
                    "few_shot_n_negative": int((cls.get("few_shot") or {}).get("n_negative", 1)),
                },
            },
        },
    )


@router.post("/products/{product_id}/prompts")
async def prompts_save(product_id: str, request: Request):
    product_dir = core._product_dir_for(product_id)
    form = await request.form()

    def _int(name: str, default: int) -> int:
        try:
            return int(form.get(name) or default)
        except (TypeError, ValueError):
            return default

    new_doc = {
        "relevance": {
            "system": (form.get("relevance.system") or "").rstrip("\n") + "\n",
            "few_shot": {
                "enabled": form.get("relevance.few_shot_enabled") == "on",
                "n_positive": _int("relevance.few_shot_n_positive", 3),
                "n_negative": _int("relevance.few_shot_n_negative", 2),
            },
            "template": (form.get("relevance.template") or "").rstrip("\n") + "\n",
        },
        "classify": {
            "system": (form.get("classify.system") or "").rstrip("\n") + "\n",
            "extras_instructions": (form.get("classify.extras_instructions") or "").rstrip("\n"),
            "few_shot": {
                "enabled": form.get("classify.few_shot_enabled") == "on",
                "n_positive": _int("classify.few_shot_n_positive", 2),
                "n_negative": _int("classify.few_shot_n_negative", 1),
            },
            "template": (form.get("classify.template") or "").rstrip("\n") + "\n",
        },
    }

    # Cheap pre-check: the templates must at least be non-empty.
    errors = []
    if not new_doc["relevance"]["template"].strip():
        errors.append("relevance.template is required (it's the user-prompt the LLM sees).")
    if not new_doc["classify"]["template"].strip():
        errors.append("classify.template is required.")
    if errors:
        return RedirectResponse(
            url=f"/products/{product_id}/prompts?error=" + " | ".join(errors)[:300],
            status_code=303,
        )

    prompts_path = product_dir / "prompts.yaml"
    backup_path = prompts_path.with_suffix(".yaml.bak")
    if prompts_path.exists():
        prompts_path.replace(backup_path)
    try:
        prompts_path.write_text(
            yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        core.clear_cache()
        load_product(product_id)
    except Exception as e:
        if prompts_path.exists():
            prompts_path.unlink()
        if backup_path.exists():
            backup_path.replace(prompts_path)
        core.clear_cache()
        return RedirectResponse(
            url=f"/products/{product_id}/prompts?error={str(e)[:200]}",
            status_code=303,
        )
    if backup_path.exists():
        backup_path.unlink()
    # POST_V1_PLAN §4.5 — bump version + archive on every explicit save so
    # history/A-B testing works from day one.
    try:
        from pipeline import prompt_versioning as _pv
        _pv.bump_version(product_dir, new_doc)
    except Exception:
        pass  # versioning failure never blocks the save
    return RedirectResponse(url=f"/products/{product_id}/prompts?saved=1", status_code=303)


# --- Digest v2 report config (report_v2_design.md §7.4) --------------------
#
# Per-product editor for products/<id>/report_config.yaml. Section toggles,
# sentiment thresholds, headline top-N. Competitor list is edited on the
# product profile page (it's a product fact, not a report setting).


@router.get("/products/{product_id}/report", response_class=HTMLResponse)
def report_config_form(request: Request, product_id: str,
                       saved: Optional[str] = None, error: Optional[str] = None):
    from pipeline import features as _features
    from pipeline import report_config as _rc
    product = core._product_or_404(product_id)
    if not _features.enabled("digest_v2_enabled", product_id):
        return core.templates.TemplateResponse(
            "base.html",
            {
                "request": request,
                "content": (
                    "<h2>Digest v2 not enabled</h2><p>Flip "
                    "<code>digest_v2_enabled: true</code> in "
                    "<code>config/features.yaml</code> to configure the digest.</p>"
                ),
            },
        )
    return core.templates.TemplateResponse(
        "product_report.html",
        {
            "request": request,
            "product": product,
            "cfg": _rc.load(product_id),
            "config_path": str(_rc.path_for(product_id)),
            "saved": saved,
            "error": error,
        },
    )


@router.post("/products/{product_id}/report")
async def report_config_save(product_id: str, request: Request):
    from pipeline import report_config as _rc
    core._product_or_404(product_id)
    form = await request.form()

    def _float(name: str, default: float) -> float:
        try:
            return float(form.get(name) or default)
        except (TypeError, ValueError):
            return default

    def _int(name: str, default: int) -> int:
        try:
            return int(form.get(name) or default)
        except (TypeError, ValueError):
            return default

    new_doc = {
        "digest_v2": {
            "sections": {
                "positive": form.get("sections.positive") == "on",
                "negative": form.get("sections.negative") == "on",
                "bugs": form.get("sections.bugs") == "on",
                "features": form.get("sections.features") == "on",
                "competition": form.get("sections.competition") == "on",
            },
            "sentiment_thresholds": {
                "positive": _float("thresholds.positive", 0.2),
                "negative": _float("thresholds.negative", -0.2),
            },
            "headline_top_n": _int("headline_top_n", 25),
        },
    }

    # Validate thresholds are sane before writing.
    if new_doc["digest_v2"]["sentiment_thresholds"]["positive"] <= \
       new_doc["digest_v2"]["sentiment_thresholds"]["negative"]:
        return RedirectResponse(
            url=f"/products/{product_id}/report?error=positive+threshold+must+be+greater+than+negative",
            status_code=303,
        )

    path = _rc.path_for(product_id)
    backup = path.with_suffix(".yaml.bak")
    if path.exists():
        path.replace(backup)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True,
                           default_flow_style=False),
            encoding="utf-8",
        )
    except Exception as e:
        if path.exists():
            path.unlink()
        if backup.exists():
            backup.replace(path)
        return RedirectResponse(
            url=f"/products/{product_id}/report?error={str(e)[:200]}",
            status_code=303,
        )
    if backup.exists():
        backup.unlink()
    return RedirectResponse(url=f"/products/{product_id}/report?saved=1", status_code=303)


# --- Prompt suggestions (POST_V1_PLAN §4.5) ---------------------------------
#
# Assistant LLM analyzes recent snippets + current prompt and proposes a
# small ordered list of edits. Each suggestion is coverage-checked against
# the last 30 days of items; edits that would drop relevance pass rate by
# > 10pp are flagged red and blocked.
#
# Feature-flagged by `prompt_suggestions_enabled`. Depends on the
# assistant LLM being configured.


@router.get("/products/{product_id}/prompts/suggestions", response_class=HTMLResponse)
def prompts_suggestions(request: Request, product_id: str):
    from pipeline import features as _features
    product = core._product_or_404(product_id)
    if not _features.enabled("prompt_suggestions_enabled", product_id):
        return core.templates.TemplateResponse(
            "prompt_suggestions.html",
            {"request": request, "product": product, "flag_off": True,
             "suggestions": None, "coverage_results": [], "error": None},
        )

    from pipeline import prompt_suggestions as _ps
    current_prompts = dict(product.prompts or {})
    # Recent snippets = added AFTER the current prompt was last updated.
    # For a first implementation, use the last 20 snippets ordered by
    # created_at desc (§4.4 snippets carry created_at).
    recent = sorted(
        (s for s in product.snippets if s.created_at is not None),
        key=lambda s: s.created_at, reverse=True,
    )[:20]

    if not recent:
        return core.templates.TemplateResponse(
            "prompt_suggestions.html",
            {"request": request, "product": product, "flag_off": False,
             "suggestions": None, "coverage_results": [], "recent_count": 0,
             "error": ("No snippets to review yet. Add positive/negative "
                       "examples on the Snippets page first.")},
        )

    result = _ps.generate_suggestions(
        current_prompts=current_prompts,
        recent_snippets=recent,
    )
    if result is None:
        return core.templates.TemplateResponse(
            "prompt_suggestions.html",
            {"request": request, "product": product, "flag_off": False,
             "suggestions": None, "coverage_results": [], "recent_count": len(recent),
             "error": ("Assistant LLM is unavailable or returned no suggestions. "
                       "Check /connections/assistant_llm.")},
        )

    # Per-edit coverage check
    coverage_results = []
    for edit in result.edits:
        try:
            proposed = _ps.apply_edit(current_prompts, edit)
        except ValueError as e:
            coverage_results.append({"error": str(e), "coverage": None})
            continue
        cov = _ps.coverage_check(
            product_id=product_id,
            baseline_prompts=current_prompts,
            proposed_prompts=proposed,
        )
        coverage_results.append({"error": None, "coverage": cov})

    return core.templates.TemplateResponse(
        "prompt_suggestions.html",
        {"request": request, "product": product, "flag_off": False,
         "suggestions": result, "coverage_results": coverage_results,
         "recent_count": len(recent), "error": None},
    )


@router.post("/products/{product_id}/prompts/suggestions/apply")
async def prompts_suggestions_apply(product_id: str, request: Request):
    """Apply one or more selected edits to prompts.yaml + archive the
    prior version. Each posted `edit_<i>_kind`/`_target`/`_before`/`_after`
    describes one edit; the checkbox `apply_<i>` selects which to keep."""
    from pipeline import features as _features
    if not _features.enabled("prompt_suggestions_enabled", product_id):
        raise HTTPException(status_code=403, detail="prompt_suggestions is disabled")

    product = core._product_or_404(product_id)
    product_dir = core._product_dir_for(product_id)
    from pipeline import prompt_suggestions as _ps
    from pipeline import prompt_versioning as _pv
    from pipeline.prompt_suggestions import PromptEdit

    form = dict(await request.form())
    selected: list[PromptEdit] = []
    i = 0
    while True:
        kind_key = f"edit_{i}_kind"
        if kind_key not in form:
            break
        if form.get(f"apply_{i}") == "on":
            try:
                selected.append(PromptEdit(
                    kind=form[kind_key],
                    target=form.get(f"edit_{i}_target", ""),
                    before=form.get(f"edit_{i}_before", ""),
                    after=form.get(f"edit_{i}_after", ""),
                    rationale=form.get(f"edit_{i}_rationale", ""),
                ))
            except Exception as e:
                return RedirectResponse(
                    url=f"/products/{product_id}/prompts?error=invalid+edit+{i}:+{str(e)[:120]}",
                    status_code=303,
                )
        i += 1

    if not selected:
        return RedirectResponse(
            url=f"/products/{product_id}/prompts?error=no+edits+selected",
            status_code=303,
        )

    current = dict(product.prompts or {})
    try:
        for e in selected:
            current = _ps.apply_edit(current, e)
    except ValueError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/prompts?error={str(e)[:200]}",
            status_code=303,
        )

    _pv.bump_version(product_dir, current)
    core.clear_cache()
    load_product(product_id)
    return RedirectResponse(
        url=f"/products/{product_id}/prompts?saved=1",
        status_code=303,
    )


# --- Taxonomy form (Phase 3) ------------------------------------------------
#
# Form-based editor for the Product -> Area -> Feature hierarchy. The user
# adds / edits / removes areas and the features under each, plus per-area
# keywords + entity_type_hint. Features are the leaf with display + description
# (description is the prompt that tells the LLM what to look for).
#
# Save flow: client serializes the tree to JSON, POSTs to
# /products/{id}/taxonomy. Server validates (>=1 feature per area, unique
# ids), rewrites taxonomy.yaml, bumps `version` to today, clears cache,
# returns {ok: true} or 422 with details.


@router.get("/products/{product_id}/taxonomy", response_class=HTMLResponse)
def taxonomy_form(request: Request, product_id: str):
    product = core._product_or_404(product_id)
    areas = product.taxonomy.get("areas") or []
    return core.templates.TemplateResponse(
        "taxonomy_form.html",
        {
            "request": request,
            "product": product,
            "areas": areas,
            "version": product.taxonomy_version,
        },
    )


@router.post("/products/{product_id}/taxonomy")
def taxonomy_save(product_id: str, payload: dict = Body(...)):
    product_dir = core._product_dir_for(product_id)
    areas_in = payload.get("areas") or []

    # Validate.
    errors: list[str] = []
    seen_area_ids: set[str] = set()
    cleaned_areas: list[dict] = []
    for ai, a in enumerate(areas_in):
        aid = (a.get("id") or "").strip().lower().replace(" ", "-")
        adisplay = (a.get("display") or "").strip()
        if not aid:
            errors.append(f"area {ai+1}: id is required")
            continue
        if aid in seen_area_ids:
            errors.append(f"area '{aid}' (#{ai+1}): duplicate id")
            continue
        seen_area_ids.add(aid)
        if not adisplay:
            errors.append(f"area '{aid}': display name is required")
            continue
        feats_in = a.get("features") or []
        if not feats_in:
            errors.append(f"area '{aid}': at least one feature is required")
            continue
        seen_feat_ids: set[str] = set()
        cleaned_feats: list[dict] = []
        for fi, f in enumerate(feats_in):
            fid = (f.get("id") or "").strip().lower().replace(" ", "-")
            fdisplay = (f.get("display") or "").strip()
            fdesc = (f.get("description") or "").strip()
            if not fid:
                errors.append(f"area '{aid}' feature {fi+1}: id is required")
                continue
            if fid in seen_feat_ids:
                errors.append(f"area '{aid}' feature '{fid}': duplicate id within area")
                continue
            seen_feat_ids.add(fid)
            if not fdisplay:
                errors.append(f"area '{aid}' feature '{fid}': display name is required")
                continue
            if not fdesc:
                errors.append(f"area '{aid}' feature '{fid}': description is required (it's the LLM prompt)")
                continue
            cleaned_feats.append({"id": fid, "display": fdisplay, "description": fdesc})

        def _split_list(raw: Any) -> list[str]:
            if isinstance(raw, list):
                return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
            if isinstance(raw, str):
                return [s.strip() for s in raw.split(",") if s.strip()]
            return []

        cleaned_areas.append({
            "id": aid,
            "display": adisplay,
            "enabled": bool(a.get("enabled", True)),
            "keywords": _split_list(a.get("keywords")),
            "entity_type_hint": _split_list(a.get("entity_type_hint")),
            "features": cleaned_feats,
        })

    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    if not cleaned_areas:
        raise HTTPException(status_code=422, detail={"errors": ["at least one area is required"]})

    # Write back to taxonomy.yaml. Bump version to today so trend continuity
    # markers show a discontinuity.
    new_doc = {
        "version": date.today().isoformat(),
        "areas": cleaned_areas,
    }
    taxonomy_path = product_dir / "taxonomy.yaml"
    backup_path = taxonomy_path.with_suffix(".yaml.bak")
    if taxonomy_path.exists():
        taxonomy_path.replace(backup_path)
    try:
        taxonomy_path.write_text(
            yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        core.clear_cache()
        # Validate by reloading.
        load_product(product_id)
    except Exception as e:
        # Roll back.
        if taxonomy_path.exists():
            taxonomy_path.unlink()
        if backup_path.exists():
            backup_path.replace(taxonomy_path)
        core.clear_cache()
        raise HTTPException(status_code=422, detail={"errors": [str(e)]})
    if backup_path.exists():
        backup_path.unlink()
    return {"ok": True, "version": new_doc["version"]}


# --- YAML editors (UI 2) ----------------------------------------------------
#
# Each per-product YAML file (sources.yaml, prompts.yaml, taxonomy.yaml,
# llm_routing.yaml) has the same shape of editor:
#
#   GET  /products/{id}/<thing>          render YAML in a textarea
#   POST /products/{id}/<thing>          parse + validate (via product re-load),
#                                        write file on success, redirect back
#
# Validation strategy: write to a temp file, attempt to YAML-parse it, attempt
# to re-load the product with the new content swapped in (catches schema-level
# issues for sources/prompts/etc.), commit on success.

_EDITORS = {
    "sources": {
        "filename": "sources.yaml",
        "title": "Sources",
        "help": "Source instances and their per-stream config. `type` must match a registered plugin.",
    },
    "prompts": {
        "filename": "prompts.yaml",
        "title": "Prompts",
        "help": "Relevance + classify prompt templates. Placeholders: {product_display}, {title}, {body}, {areas}, {content_types}, {few_shot_block}, {build_numbers}, {parent_block}, {extras_instructions}.",
    },
    "taxonomy": {
        "filename": "taxonomy.yaml",
        "title": "Taxonomy",
        "help": "Functional areas. Bump `version` when you edit so trend charts can mark a discontinuity.",
    },
    "llm_routing": {
        "filename": "llm_routing.yaml",
        "title": "LLM routing",
        "help": "Per-stage adapter config (endpoint, model, temperature, seed).",
    },
}


@router.get("/products/{product_id}/edit/{section}", response_class=HTMLResponse)
def yaml_editor(request: Request, product_id: str, section: str, error: Optional[str] = None):
    if section not in _EDITORS:
        raise HTTPException(status_code=404, detail=f"unknown section: {section}")
    meta = _EDITORS[section]
    product_dir = core._product_dir_for(product_id)
    file_path = product_dir / meta["filename"]
    body = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    return core.templates.TemplateResponse(
        "yaml_editor.html",
        {
            "request": request,
            "product_id": product_id,
            "section": section,
            "title": meta["title"],
            "filename": meta["filename"],
            "help": meta["help"],
            "body": body,
            "error": error,
        },
    )


@router.post("/products/{product_id}/edit/{section}")
def yaml_editor_save(product_id: str, section: str, body: str = Form(...)):
    if section not in _EDITORS:
        raise HTTPException(status_code=404, detail=f"unknown section: {section}")
    meta = _EDITORS[section]
    product_dir = core._product_dir_for(product_id)
    file_path = product_dir / meta["filename"]

    # 1. Parse YAML — surface syntax errors back to the editor.
    try:
        yaml.safe_load(body)
    except yaml.YAMLError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/edit/{section}?error=YAML+parse+error:+{str(e)[:120]}",
            status_code=303,
        )

    # 2. Write atomically (write to tmp, swap).
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")

    # 3. Reload-validate. If load_product raises, roll back.
    core.clear_cache()
    backup = None
    if file_path.exists():
        backup = file_path.with_suffix(file_path.suffix + ".bak")
        file_path.replace(backup)
    tmp.replace(file_path)
    try:
        load_product(product_id)
    except Exception as e:
        # Roll back.
        file_path.unlink(missing_ok=True)
        if backup is not None:
            backup.replace(file_path)
        core.clear_cache()
        msg = str(e)[:150].replace("+", " ")
        return RedirectResponse(
            url=f"/products/{product_id}/edit/{section}?error=Validation+failed:+{msg}",
            status_code=303,
        )

    if backup is not None and backup.exists():
        backup.unlink()
    return RedirectResponse(url=f"/products/{product_id}/edit/{section}?error=", status_code=303)
