"""FastAPI admin app, bound to 127.0.0.1.

Admin tool to manage all products (a.k.a. search topics — Product -> Area
-> Feature hierarchy) and view their generated reports. Routes cover:
product list/create + dashboard, taxonomy / sources / prompts / vendors /
llm-routing editors, snippet add/list/edit, run trigger + report viewer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
import json as _json
import subprocess
import sys

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import yaml

from pipeline.models import CONTENT_TYPES, SEVERITY_VALUES
from pipeline.snippets import (
    NEGATIVE,
    POSITIVE,
    Snippet,
    delete_snippet,
    load_snippets,
    save_snippet,
    slugify,
)
from pipeline.config import app_config, resolve_path
from datetime import date
from fastapi import Body

from dotenv import dotenv_values, set_key, unset_key

from pipeline.product import (
    PRODUCTS_DIR,
    available_products,
    clear_cache,
    load_product,
    save_product_meta,
    scaffold_product,
)

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"

app = FastAPI(title="Customer Feedback Monitor")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Index: list + create product -------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    products = []
    for pid in available_products():
        try:
            p = load_product(pid)
            products.append({
                "id": p.id,
                "display": p.display,
                "description": p.description,
                "n_sources": len(p.sources),
                "n_areas": len(p.area_ids()),
                "n_features": sum(len(a.get("features") or []) for a in p.enabled_areas()),
                "n_snippets": len(p.snippets),
            })
        except Exception as e:
            products.append({"id": pid, "display": pid, "error": str(e)})
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "products": products},
    )


@app.post("/products")
def create_product(
    product_id: str = Form(...),
    display: str = Form(...),
    description: str = Form(""),
):
    product_id = product_id.strip().lower().replace(" ", "-")
    display = display.strip()
    if not product_id or not display:
        raise HTTPException(status_code=400, detail="product_id and display are required")
    try:
        scaffold_product(product_id, display, description.strip())
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=f"/products/{product_id}", status_code=303)


# --- Per-product dashboard --------------------------------------------------


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product_dashboard(request: Request, product_id: str):
    try:
        p = load_product(product_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    sources_summary = []
    for src in p.sources:
        streams = src.get("streams", []) or []
        sources_summary.append({
            "id": src.get("id"),
            "type": src.get("type"),
            "n_streams": len(streams),
        })

    n_positive = sum(1 for s in p.snippets if s.is_positive)
    n_negative = sum(1 for s in p.snippets if s.is_negative)
    n_holdout = sum(1 for s in p.snippets if s.holdout_eval)
    n_features = sum(len(a.get("features") or []) for a in p.enabled_areas())

    return templates.TemplateResponse(
        "product.html",
        {
            "request": request,
            "product": {
                "id": p.id,
                "display": p.display,
                "description": p.description,
                "extras_class": p.extras_cls.__name__,
                "taxonomy_version": p.taxonomy_version,
                "n_areas": len(p.area_ids()),
                "n_features": n_features,
                "areas_preview": p.area_ids()[:6],
            },
            "sources_summary": sources_summary,
            "snippet_stats": {
                "total": len(p.snippets),
                "positive": n_positive,
                "negative": n_negative,
                "holdout": n_holdout,
            },
        },
    )


# --- Product metadata editor (Phase 4) --------------------------------------


@app.get("/products/{product_id}/edit/meta", response_class=HTMLResponse)
def product_meta_form(request: Request, product_id: str, error: Optional[str] = None):
    p = _product_or_404(product_id)
    return templates.TemplateResponse(
        "product_meta_form.html",
        {
            "request": request,
            "product": {
                "id": p.id,
                "display": p.display,
                "description": p.description,
                "schedule": p.product_meta.get("schedule") or "weekly",
            },
            "error": error,
        },
    )


@app.post("/products/{product_id}/edit/meta")
def product_meta_save(
    product_id: str,
    display: str = Form(...),
    description: str = Form(""),
    schedule: str = Form("weekly"),
):
    _product_or_404(product_id)
    display = display.strip()
    if not display:
        return RedirectResponse(
            url=f"/products/{product_id}/edit/meta?error=Display+name+is+required",
            status_code=303,
        )
    try:
        save_product_meta(product_id, display, description, schedule)
    except Exception as e:
        return RedirectResponse(
            url=f"/products/{product_id}/edit/meta?error={str(e)[:120]}",
            status_code=303,
        )
    return RedirectResponse(url=f"/products/{product_id}", status_code=303)


# --- Connections (per-source-type connection params, global) ----------------
#
# Connections are credentials / endpoint settings that belong to a source
# TYPE, not to a per-product source instance. They live in the project-root
# .env file (which python-dotenv reads at process start). The connection
# editor reads + writes that file in place via dotenv.set_key, preserving
# any unrelated keys + comments.

ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"

CONNECTION_META: dict[str, dict] = {
    "reddit": {
        "display": "Reddit",
        "help": (
            "Reddit Data API (non-commercial). Register a Script-type app at "
            "https://www.reddit.com/prefs/apps and put the client id + secret "
            "below. Approval can take 2-4 weeks — see "
            "documents/REDDIT_APPROVAL_PLAN.md."
        ),
        "fields": [
            {"env": "REDDIT_CLIENT_ID", "label": "Client ID", "type": "text", "default": "",
             "help": "The short string under the app name (under 'personal use script') on the prefs/apps page."},
            {"env": "REDDIT_CLIENT_SECRET", "label": "Client Secret", "type": "secret", "default": "",
             "help": "The 'secret' field on the app registration. Treated as a credential."},
            {"env": "REDDIT_USER_AGENT", "label": "User Agent", "type": "text",
             "default": "customer-feedback-monitor:0.1 (by /u/yourname)",
             "help": "Reddit-mandated format: <platform>:<app-id>:<version> (by /u/<username>). Non-conforming UAs are rate-limited or blocked."},
        ],
    },
    "github_issues": {
        "display": "GitHub Issues",
        "help": (
            "GitHub REST API for public issue trackers. Create a fine-grained "
            "PAT at https://github.com/settings/tokens?type=beta with "
            "permissions: Public Repositories (read-only). No approval needed."
        ),
        "fields": [
            {"env": "GITHUB_TOKEN", "label": "Personal Access Token (PAT)", "type": "secret", "default": "",
             "help": "Fine-grained PAT, public-repos read access. Starts with 'github_pat_…'. Treated as a credential."},
        ],
    },
    "hn": {
        "display": "Hacker News",
        "help": (
            "Algolia-hosted HN search index. No authentication required and no "
            "rate-limit ceiling for fair-use traffic. Nothing to configure here."
        ),
        "fields": [],
    },
    "microsoft_community": {
        "display": "Microsoft Tech Community (RSS)",
        "help": (
            "Public RSS feeds. No authentication required. Nothing to configure "
            "here. Verify your feed URLs in each product's Sources page."
        ),
        "fields": [],
    },
}


def _read_env() -> dict[str, str]:
    """Return current values in .env (empty dict if the file doesn't exist)."""
    if not ENV_FILE_PATH.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(str(ENV_FILE_PATH)).items()}


def _connection_status(type_id: str, env: dict[str, str]) -> str:
    """One-word status for the list view: configured / partial / not-needed / missing."""
    meta = CONNECTION_META.get(type_id, {})
    fields = meta.get("fields") or []
    if not fields:
        return "not-needed"
    set_fields = sum(1 for f in fields if (env.get(f["env"]) or "").strip())
    if set_fields == len(fields):
        return "configured"
    if set_fields == 0:
        return "missing"
    return "partial"


@app.get("/connections", response_class=HTMLResponse)
def connections_index(request: Request):
    env = _read_env()
    from sources import available_source_types
    available = available_source_types()
    rows = []
    for type_id in available:
        meta = CONNECTION_META.get(type_id, {"display": type_id, "fields": []})
        rows.append({
            "type": type_id,
            "display": meta.get("display") or type_id,
            "n_fields": len(meta.get("fields") or []),
            "status": _connection_status(type_id, env),
        })
    return templates.TemplateResponse(
        "connections_index.html",
        {"request": request, "rows": rows, "env_file": str(ENV_FILE_PATH)},
    )


@app.get("/connections/{type_id}", response_class=HTMLResponse)
def connection_form(request: Request, type_id: str, error: Optional[str] = None, saved: Optional[str] = None):
    if type_id not in CONNECTION_META:
        raise HTTPException(status_code=404, detail=f"unknown source type: {type_id}")
    meta = CONNECTION_META[type_id]
    env = _read_env()
    values: dict[str, str] = {}
    for f in meta.get("fields") or []:
        values[f["env"]] = env.get(f["env"], "") or f.get("default", "")
    return templates.TemplateResponse(
        "connection_form.html",
        {
            "request": request,
            "type_id": type_id,
            "meta": meta,
            "values": values,
            "error": error,
            "saved": saved,
            "env_file": str(ENV_FILE_PATH),
        },
    )


@app.post("/connections/{type_id}")
async def connection_save(type_id: str, request: Request):
    if type_id not in CONNECTION_META:
        raise HTTPException(status_code=404, detail=f"unknown source type: {type_id}")
    meta = CONNECTION_META[type_id]
    form = await request.form()

    # Make sure the .env file exists; dotenv.set_key creates it if absent
    # but its parent must exist. Project root always does.
    ENV_FILE_PATH.touch(exist_ok=True)

    try:
        for f in meta.get("fields") or []:
            env_name = f["env"]
            new_val = (form.get(env_name) or "").strip()
            # Empty value -> unset the key entirely (cleaner than KEY=)
            if new_val == "":
                # unset_key tolerates absent keys
                try:
                    unset_key(str(ENV_FILE_PATH), env_name)
                except Exception:
                    pass
            else:
                set_key(str(ENV_FILE_PATH), env_name, new_val, quote_mode="auto")
    except Exception as e:
        return RedirectResponse(
            url=f"/connections/{type_id}?error={str(e)[:200]}",
            status_code=303,
        )

    return RedirectResponse(url=f"/connections/{type_id}?saved=1", status_code=303)


# --- Sources form (Phase 5) -------------------------------------------------
#
# Per-type form layout so a non-YAML user can add / remove source instances
# and their streams. Type-specific stream fields are described here and
# rendered by the template via <template> tags. Server-side validation lives
# in the POST handler — it knows the field shape per type and errors loud
# (422 with detail.errors[]) if anything is missing.

# Type metadata drives:
#   - the "Add source" picker (only registered types are offered)
#   - the per-type help text in the form
#   - the server-side validation of stream rows
#
# Each entry describes the per-stream fields and their help text.

SOURCE_TYPE_META: dict[str, dict] = {
    "reddit": {
        "display": "Reddit",
        "help": (
            "Subreddit-based ingest via PRAW. Needs Reddit non-commercial API "
            "approval and REDDIT_CLIENT_ID/SECRET in .env. Each stream is one "
            "subreddit."
        ),
        "stream_fields": [
            {"name": "subreddit", "label": "Subreddit", "type": "text", "required": True,
             "placeholder": "Windows11", "help": "Subreddit name, no r/ prefix."},
            {"name": "display", "label": "Display label", "type": "text", "required": False,
             "placeholder": "r/Windows11",
             "help": "Human-readable name shown in reports. Defaults to r/<subreddit>."},
            {"name": "engagement_threshold", "label": "Engagement threshold", "type": "number", "required": False, "default": 5,
             "help": "Minimum upvotes+comments needed for an item to survive the heuristic filter. Lower = more items + more noise."},
        ],
    },
    "hn": {
        "display": "Hacker News",
        "help": (
            "Algolia-backed HN search. No auth. Each stream is a list of "
            "search queries the connector iterates."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "hn-windows", "help": "Internal label for cursor / dedup; doesn't have to be unique across products."},
            {"name": "search_queries", "label": "Search queries (one per line)", "type": "textarea_list", "required": True,
             "placeholder": "windows 11\nKB5036980\nmicrosoft copilot",
             "help": "One Lucene-style query per line. Each is paginated independently."},
            {"name": "include_tags", "label": "Include tags (comma list)", "type": "csv", "required": False, "default": "story",
             "help": "story | comment | story,comment. story-only avoids comment-without-parent-context noise."},
            {"name": "max_pages_per_query", "label": "Max pages per query", "type": "number", "required": False, "default": 5,
             "help": "Algolia caps at 1000 results per query; a page is `hits_per_page` items."},
            {"name": "hits_per_page", "label": "Hits per page", "type": "number", "required": False, "default": 100,
             "help": "1-200. 100 is the recommended sweet spot."},
        ],
    },
    "github_issues": {
        "display": "GitHub Issues",
        "help": (
            "GitHub REST /repos/{owner}/{repo}/issues. Needs a fine-grained "
            "PAT in .env as GITHUB_TOKEN (Public Repositories, read-only). "
            "Each stream is a set of repos."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "microsoft-dev-tools", "help": "Internal label for cursor / dedup."},
            {"name": "repos", "label": "Repos (one per line, owner/repo)", "type": "textarea_list", "required": True,
             "placeholder": "microsoft/PowerToys\nmicrosoft/terminal\nmicrosoft/WSL",
             "help": "Each line is one repo. Cursor advances on MAX(updated_at) across them; dedup catches the small overlap."},
            {"name": "include_labels", "label": "Include only labels (comma list)", "type": "csv", "required": False, "default": "",
             "help": "Empty = all issues. If set, only issues with at least one of these labels are kept."},
            {"name": "exclude_labels", "label": "Exclude labels (comma list)", "type": "csv", "required": False, "default": "duplicate,wontfix",
             "help": "Drop issues with any of these labels. Defaults exclude obvious noise."},
            {"name": "fetch_comments", "label": "Fetch comments", "type": "bool", "required": False, "default": True,
             "help": "Fetch comments on each issue. Adds API calls but gives the classifier more context."},
            {"name": "max_comments_per_issue", "label": "Max comments per issue", "type": "number", "required": False, "default": 50,
             "help": "Safety cap on hot threads. Older comments past the cap are dropped."},
        ],
    },
    "microsoft_community": {
        "display": "Microsoft Tech Community (RSS)",
        "help": (
            "Lithium-platform RSS for Microsoft Tech Community + Q&A. No auth. "
            "Each stream is one feed URL. Verify URLs against the live site — "
            "they break after redesigns."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "tech-community-windows", "help": "Internal label for cursor / dedup."},
            {"name": "display", "label": "Display label", "type": "text", "required": False,
             "placeholder": "Tech Community — Windows",
             "help": "Human-readable name shown in reports."},
            {"name": "feed_url", "label": "Feed URL", "type": "text", "required": True,
             "placeholder": "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/Category?category.id=Windows",
             "help": "The full RSS URL. The Windows category URL is the example shown."},
        ],
    },
}


@app.get("/products/{product_id}/sources", response_class=HTMLResponse)
def sources_form(request: Request, product_id: str):
    product = _product_or_404(product_id)
    from sources import available_source_types

    available = available_source_types()
    # Only offer types we have plugin AND metadata for.
    offerable = [t for t in available if t in SOURCE_TYPE_META]
    return templates.TemplateResponse(
        "sources_form.html",
        {
            "request": request,
            "product": product,
            "sources": product.sources,
            "type_meta": SOURCE_TYPE_META,
            "offerable_types": offerable,
        },
    )


@app.post("/products/{product_id}/sources")
def sources_save(product_id: str, payload: dict = Body(...)):
    product_dir = _product_dir_for(product_id)
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
        if stype not in SOURCE_TYPE_META:
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

        fields = SOURCE_TYPE_META[stype]["stream_fields"]
        cleaned_streams: list[dict] = []
        for sti, stream in enumerate(streams_in):
            clean_stream: dict = {}
            for field in fields:
                value = _coerce(field, stream.get(field["name"]))
                if field.get("required") and not value and value != 0 and value is not False:
                    errors.append(
                        f"source '{sid}' stream #{sti+1}: '{field['label']}' is required"
                    )
                if value is None or value == "" or value == []:
                    continue
                clean_stream[field["name"]] = value
            cleaned_streams.append(clean_stream)

        cleaned.append({
            "id": sid,
            "type": stype,
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
        clear_cache()
        load_product(product_id)
    except Exception as e:
        if sources_path.exists():
            sources_path.unlink()
        if backup_path.exists():
            backup_path.replace(sources_path)
        clear_cache()
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
        ("{content_types}", "Comma-separated content-type vocabulary."),
        ("{extras_instructions}", "Free-form per-product notes (from the field below)."),
        ("{few_shot_block}", "Auto-rendered few-shot examples (when few_shot.enabled is true and the product has snippets)."),
        ("{vendor_hits}", "Comma list of vendor names matched by the regex pre-pass."),
        ("{kb_numbers}", "Comma list of KB numbers matched by regex."),
        ("{build_numbers}", "Comma list of Windows-build-style numbers matched by regex."),
        ("{parent_block}", "For comments: the parent post title + body excerpt (auto-filled)."),
        ("{title}", "Item's title."),
        ("{body}", "Item's body, truncated to 4000 chars."),
        ("{engagement}", "Item engagement metrics JSON."),
        ("{source}", "Display name of the source instance."),
    ],
}


@app.get("/products/{product_id}/prompts", response_class=HTMLResponse)
def prompts_form(request: Request, product_id: str, saved: Optional[str] = None, error: Optional[str] = None):
    product = _product_or_404(product_id)
    prompts = product.prompts or {}
    rel = prompts.get("relevance") or {}
    cls = prompts.get("classify") or {}
    return templates.TemplateResponse(
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


@app.post("/products/{product_id}/prompts")
async def prompts_save(product_id: str, request: Request):
    product_dir = _product_dir_for(product_id)
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
        clear_cache()
        load_product(product_id)
    except Exception as e:
        if prompts_path.exists():
            prompts_path.unlink()
        if backup_path.exists():
            backup_path.replace(prompts_path)
        clear_cache()
        return RedirectResponse(
            url=f"/products/{product_id}/prompts?error={str(e)[:200]}",
            status_code=303,
        )
    if backup_path.exists():
        backup_path.unlink()
    return RedirectResponse(url=f"/products/{product_id}/prompts?saved=1", status_code=303)


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


@app.get("/products/{product_id}/taxonomy", response_class=HTMLResponse)
def taxonomy_form(request: Request, product_id: str):
    product = _product_or_404(product_id)
    areas = product.taxonomy.get("areas") or []
    return templates.TemplateResponse(
        "taxonomy_form.html",
        {
            "request": request,
            "product": product,
            "areas": areas,
            "version": product.taxonomy_version,
        },
    )


@app.post("/products/{product_id}/taxonomy")
def taxonomy_save(product_id: str, payload: dict = Body(...)):
    product_dir = _product_dir_for(product_id)
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
        clear_cache()
        # Validate by reloading.
        load_product(product_id)
    except Exception as e:
        # Roll back.
        if taxonomy_path.exists():
            taxonomy_path.unlink()
        if backup_path.exists():
            backup_path.replace(taxonomy_path)
        clear_cache()
        raise HTTPException(status_code=422, detail={"errors": [str(e)]})
    if backup_path.exists():
        backup_path.unlink()
    return {"ok": True, "version": new_doc["version"]}


# --- YAML editors (UI 2) ----------------------------------------------------
#
# Each per-product YAML file (sources.yaml, prompts.yaml, taxonomy.yaml,
# vendors.yaml, llm_routing.yaml) has the same shape of editor:
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
        "help": "Relevance + classify prompt templates. Placeholders: {product_display}, {title}, {body}, {areas}, {content_types}, {few_shot_block}, {vendor_hits}, {kb_numbers}, {build_numbers}, {parent_block}, {extras_instructions}.",
    },
    "taxonomy": {
        "filename": "taxonomy.yaml",
        "title": "Taxonomy",
        "help": "Functional areas. Bump `version` when you edit so trend charts can mark a discontinuity.",
    },
    "vendors": {
        "filename": "vendors.yaml",
        "title": "Vendors",
        "help": "Vendor + product seed list for entity extraction (regex pre-pass + LLM hints).",
    },
    "llm_routing": {
        "filename": "llm_routing.yaml",
        "title": "LLM routing",
        "help": "Per-stage adapter config (endpoint, model, temperature, seed).",
    },
}


def _product_dir_for(product_id: str) -> Path:
    d = PRODUCTS_DIR / product_id
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"product '{product_id}' not found")
    return d


@app.get("/products/{product_id}/edit/{section}", response_class=HTMLResponse)
def yaml_editor(request: Request, product_id: str, section: str, error: Optional[str] = None):
    if section not in _EDITORS:
        raise HTTPException(status_code=404, detail=f"unknown section: {section}")
    meta = _EDITORS[section]
    product_dir = _product_dir_for(product_id)
    file_path = product_dir / meta["filename"]
    body = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    return templates.TemplateResponse(
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


@app.post("/products/{product_id}/edit/{section}")
def yaml_editor_save(product_id: str, section: str, body: str = Form(...)):
    if section not in _EDITORS:
        raise HTTPException(status_code=404, detail=f"unknown section: {section}")
    meta = _EDITORS[section]
    product_dir = _product_dir_for(product_id)
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
    clear_cache()
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
        clear_cache()
        msg = str(e)[:150].replace("+", " ")
        return RedirectResponse(
            url=f"/products/{product_id}/edit/{section}?error=Validation+failed:+{msg}",
            status_code=303,
        )

    if backup is not None and backup.exists():
        backup.unlink()
    return RedirectResponse(url=f"/products/{product_id}/edit/{section}?error=", status_code=303)



# --- Snippets (UI 3) --------------------------------------------------------


def _product_or_404(product_id: str):
    try:
        return load_product(product_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _snippets_index_url(product_id: str) -> str:
    return f"/products/{product_id}/snippets"


@app.get("/products/{product_id}/snippets", response_class=HTMLResponse)
def snippets_list(request: Request, product_id: str):
    product = _product_or_404(product_id)
    snips = sorted(product.snippets, key=lambda s: (not s.is_positive, s.id))
    return templates.TemplateResponse(
        "snippets_list.html",
        {"request": request, "product": product, "snippets": snips},
    )


@app.get("/products/{product_id}/snippets/new", response_class=HTMLResponse)
def snippets_new_form(request: Request, product_id: str, mode: str = "url"):
    product = _product_or_404(product_id)
    if mode not in ("url", "text"):
        mode = "url"
    return templates.TemplateResponse(
        "snippet_form.html",
        {
            "request": request,
            "product": product,
            "mode": mode,
            "snippet": None,                 # new
            "area_ids": product.area_ids(),
            "content_types": sorted(CONTENT_TYPES),
            "severity_values": ["", *sorted(SEVERITY_VALUES)],
            "form_action": f"/products/{product_id}/snippets",
            "edit": False,
            "error": None,
        },
    )


@app.get("/products/{product_id}/snippets/{snippet_id}", response_class=HTMLResponse)
def snippets_edit_form(request: Request, product_id: str, snippet_id: str, error: Optional[str] = None):
    product = _product_or_404(product_id)
    snip = next((s for s in product.snippets if s.id == snippet_id), None)
    if snip is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    mode = "url" if snip.source_url else "text"
    return templates.TemplateResponse(
        "snippet_form.html",
        {
            "request": request,
            "product": product,
            "mode": mode,
            "snippet": snip,
            "area_ids": product.area_ids(),
            "content_types": sorted(CONTENT_TYPES),
            "severity_values": ["", *sorted(SEVERITY_VALUES)],
            "form_action": f"/products/{product_id}/snippets/{snippet_id}",
            "edit": True,
            "error": error,
        },
    )


def _build_snippet_from_form(
    *,
    product,
    snippet_id: Optional[str],
    polarity: str,
    source_url: str,
    title: str,
    body: str,
    summary: str,
    is_topic_relevant: bool,
    areas: list[str],
    content_types_in: list[str],
    sentiment: Optional[float],
    bug_severity: str,
    notes: str,
    holdout_eval: bool,
) -> Snippet:
    if polarity not in (POSITIVE, NEGATIVE):
        raise ValueError(f"polarity must be positive_example or negative_example, got {polarity!r}")
    body = (body or "").strip()
    source_url = (source_url or "").strip() or None
    if not body and not source_url:
        raise ValueError("snippet needs either a URL or pasted body text")

    labels: dict = {"is_topic_relevant": bool(is_topic_relevant)}
    if areas:
        labels["areas"] = areas
    if content_types_in:
        labels["content_types"] = content_types_in
    if sentiment is not None:
        labels["sentiment"] = sentiment
    if summary:
        labels["summary"] = summary
    if bug_severity:
        labels["bug_severity"] = bug_severity

    # Default id from title or first words of body.
    sid = snippet_id or slugify(title or body[:60] or polarity)
    # Resolve collisions with a numeric suffix.
    existing_ids = {s.id for s in product.snippets}
    if not snippet_id and sid in existing_ids:
        base = sid
        n = 2
        while f"{base}-{n}" in existing_ids:
            n += 1
        sid = f"{base}-{n}"

    return Snippet(
        id=sid,
        polarity=polarity,
        source_url=source_url,
        title=title.strip() or None,
        body=body,
        labels=labels,
        holdout_eval=bool(holdout_eval),
        notes=notes.strip(),
    )


def _parse_areas(raw: list[str]) -> list[str]:
    return [a.strip() for a in raw if a and a.strip()]


@app.post("/products/{product_id}/snippets")
async def snippets_create(product_id: str, request: Request):
    product = _product_or_404(product_id)
    form = await request.form()
    try:
        snippet = _build_snippet_from_form(
            product=product,
            snippet_id=None,
            polarity=form.get("polarity") or POSITIVE,
            source_url=form.get("source_url") or "",
            title=form.get("title") or "",
            body=form.get("body") or "",
            summary=(form.get("summary") or "").strip(),
            is_topic_relevant=form.get("is_topic_relevant") == "on",
            areas=_parse_areas(form.getlist("areas")),
            content_types_in=_parse_areas(form.getlist("content_types")),
            sentiment=float(form["sentiment"]) if form.get("sentiment") else None,
            bug_severity=(form.get("bug_severity") or "").strip(),
            notes=form.get("notes") or "",
            holdout_eval=form.get("holdout_eval") == "on",
        )
    except ValueError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/snippets/new?mode={form.get('mode', 'url')}",
            status_code=303,
        )
    product_dir = PRODUCTS_DIR / product_id
    save_snippet(product_dir, snippet)
    clear_cache()
    return RedirectResponse(url=_snippets_index_url(product_id), status_code=303)


@app.post("/products/{product_id}/snippets/{snippet_id}")
async def snippets_update(product_id: str, snippet_id: str, request: Request):
    product = _product_or_404(product_id)
    existing = next((s for s in product.snippets if s.id == snippet_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    form = await request.form()
    try:
        new_snippet = _build_snippet_from_form(
            product=product,
            snippet_id=snippet_id,  # keep the same id
            polarity=form.get("polarity") or existing.polarity,
            source_url=form.get("source_url") or "",
            title=form.get("title") or "",
            body=form.get("body") or "",
            summary=(form.get("summary") or "").strip(),
            is_topic_relevant=form.get("is_topic_relevant") == "on",
            areas=_parse_areas(form.getlist("areas")),
            content_types_in=_parse_areas(form.getlist("content_types")),
            sentiment=float(form["sentiment"]) if form.get("sentiment") else None,
            bug_severity=(form.get("bug_severity") or "").strip(),
            notes=form.get("notes") or "",
            holdout_eval=form.get("holdout_eval") == "on",
        )
    except ValueError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/snippets/{snippet_id}?error={str(e)[:120]}",
            status_code=303,
        )

    # Polarity change moves the file across directories — delete the old one
    # (in its old polarity dir) before saving the new one.
    if existing.polarity != new_snippet.polarity:
        delete_snippet(existing)

    product_dir = PRODUCTS_DIR / product_id
    save_snippet(product_dir, new_snippet)
    clear_cache()
    return RedirectResponse(url=_snippets_index_url(product_id), status_code=303)


@app.post("/products/{product_id}/snippets/{snippet_id}/delete")
def snippets_delete(product_id: str, snippet_id: str):
    product = _product_or_404(product_id)
    existing = next((s for s in product.snippets if s.id == snippet_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    delete_snippet(existing)
    clear_cache()
    return RedirectResponse(url=_snippets_index_url(product_id), status_code=303)



# --- Runs + reports (UI 4) --------------------------------------------------
#
# Run trigger spawns `python -m pipeline.run --product <id>` as a subprocess.
# Status is read from data/<product>/run_logs/<run_id>.json (written by the
# pipeline at the end) and from a sidecar .running marker file we drop before
# starting the subprocess. Stdout/stderr go to .out so the user can see what
# happened on failure.


def _product_data_root(product_id: str) -> Path:
    return resolve_path(app_config()["paths"]["data_root"]) / product_id


def _run_logs_dir(product_id: str) -> Path:
    return _product_data_root(product_id) / "run_logs"


def _reports_root_for(product_id: str) -> Path:
    return resolve_path(app_config()["paths"]["reports_root"]) / product_id


def _list_runs(product_id: str) -> list[dict]:
    """Combine completed .json run logs + still-running .running markers."""
    logs_dir = _run_logs_dir(product_id)
    rows: dict[str, dict] = {}
    if logs_dir.exists():
        for jf in logs_dir.glob("*.json"):
            try:
                payload = _json.loads(jf.read_text(encoding="utf-8"))
                rid = payload.get("run_id") or jf.stem
                rows[rid] = {
                    "run_id": rid,
                    "week_id": payload.get("week_id"),
                    "status": payload.get("status") or "unknown",
                    "stage_durations": payload.get("stage_durations") or {},
                    "counters": payload.get("counters") or {},
                    "running": False,
                }
            except Exception:
                continue
        for mk in logs_dir.glob("*.running"):
            rid = mk.stem
            rows.setdefault(rid, {
                "run_id": rid, "week_id": None, "status": "running",
                "stage_durations": {}, "counters": {}, "running": True,
            })
    return sorted(rows.values(), key=lambda r: r["run_id"], reverse=True)


def _read_run(product_id: str, run_id: str) -> Optional[dict]:
    logs = _run_logs_dir(product_id)
    jf = logs / f"{run_id}.json"
    if jf.exists():
        try:
            return _json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _run_is_running(product_id: str, run_id: str) -> bool:
    return (_run_logs_dir(product_id) / f"{run_id}.running").exists()


def _run_stdout(product_id: str, run_id: str) -> str:
    out = _run_logs_dir(product_id) / f"{run_id}.out"
    return out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""


def _report_dir_for_run(product_id: str, run_payload: Optional[dict]) -> Optional[Path]:
    if not run_payload or not run_payload.get("week_id"):
        return None
    candidate = _reports_root_for(product_id) / run_payload["week_id"]
    return candidate if (candidate / "index.html").exists() else None


@app.get("/products/{product_id}/runs", response_class=HTMLResponse)
def runs_index(request: Request, product_id: str):
    product = _product_or_404(product_id)
    runs = _list_runs(product_id)
    return templates.TemplateResponse(
        "runs_list.html",
        {"request": request, "product": product, "runs": runs},
    )


@app.post("/products/{product_id}/runs")
def runs_create(product_id: str, skip_fetch: Optional[str] = Form(None), skip_llm: Optional[str] = Form(None)):
    _product_or_404(product_id)

    # Pre-allocate a run_id so we can redirect immediately; the pipeline will
    # generate its own run_id internally too. We use ours only for the
    # .running marker so the listing shows the in-flight subprocess.
    import uuid
    from datetime import datetime, timezone
    marker_id = f"ui-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"

    logs_dir = _run_logs_dir(product_id)
    logs_dir.mkdir(parents=True, exist_ok=True)
    marker = logs_dir / f"{marker_id}.running"
    marker.write_text(f"started by webui at {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")

    out_path = logs_dir / f"{marker_id}.out"
    cmd = [sys.executable, "-m", "pipeline.run", "--product", product_id]
    if skip_fetch:
        cmd.append("--skip-fetch")
    if skip_llm:
        cmd.append("--skip-llm")

    # Fire-and-forget: subprocess writes its real run log on completion.
    # We do NOT wait. The .running marker is cleaned up by a post-run check
    # the UI runs lazily when listing/reading status.
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=open(out_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # On Windows, DETACHED_PROCESS lets the child outlive a UI restart
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) if sys.platform == "win32" else 0,
        )
    except Exception as e:
        marker.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"failed to spawn pipeline: {e}")

    # Record the marker -> pid mapping so a future stop-button could read it.
    (logs_dir / f"{marker_id}.pid").write_text(str(proc.pid), encoding="utf-8")

    return RedirectResponse(url=f"/products/{product_id}/runs/{marker_id}", status_code=303)


@app.get("/products/{product_id}/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, product_id: str, run_id: str):
    product = _product_or_404(product_id)
    payload = _read_run(product_id, run_id)
    running = _run_is_running(product_id, run_id) and payload is None
    stdout = _run_stdout(product_id, run_id)
    report_dir = _report_dir_for_run(product_id, payload)

    # Best-effort marker cleanup: if the JSON exists, the run is done; we can
    # delete the .running marker now.
    if payload is not None:
        marker = _run_logs_dir(product_id) / f"{run_id}.running"
        marker.unlink(missing_ok=True)

    return templates.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "payload": payload,
            "running": running,
            "stdout_tail": stdout[-4000:] if stdout else "",
            "report_week": (payload or {}).get("week_id") if report_dir else None,
        },
    )


@app.get("/products/{product_id}/reports/{week_id}/")
def report_index(product_id: str, week_id: str):
    return _serve_report(product_id, week_id, "index.html")


@app.get("/products/{product_id}/reports/{week_id}/{filename}")
def report_file(product_id: str, week_id: str, filename: str):
    if "/" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    return _serve_report(product_id, week_id, filename)


def _serve_report(product_id: str, week_id: str, filename: str):
    path = _reports_root_for(product_id) / week_id / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"no report file at {path}")
    return FileResponse(str(path))


# --- API: refresh caches (used by editors that mutate config) ---------------


@app.post("/api/refresh")
def refresh_caches() -> dict:
    clear_cache()
    return {"ok": True}


# --- Entry point ------------------------------------------------------------


def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    uvicorn.run(
        "webui.app:app" if reload else app,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="Customer Feedback Monitor — local admin UI")
    ap.add_argument("--host", default="127.0.0.1", help="Bind address (local-only default)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--reload", action="store_true", help="Auto-reload on file change (dev)")
    args = ap.parse_args()
    serve(args.host, args.port, args.reload)


if __name__ == "__main__":
    main()
