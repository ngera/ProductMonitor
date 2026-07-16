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
import re
import shutil
import subprocess
import sys

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, StreamingResponse
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
from pipeline.config import app_config, resolve_path, set_current_product
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


# --- Admin: tune pipeline knobs in config/app.yaml --------------------------
#
# A form-based editor over the subset of config/app.yaml that operators
# actually tune day-to-day: filter thresholds, fetch limits, grouping/scoring
# knobs. Anything not in _TUNING_FIELDS (paths, llm, ...) is left untouched
# by the save path.

_APP_YAML = Path(__file__).resolve().parent.parent / "config" / "app.yaml"

# Ordered field spec drives both the form render and the save. Each entry:
#   (section, key, type, default, help, group_label)
_TUNING_FIELDS: list[tuple] = [
    # --- Filter (heuristic drops before the LLM) ---
    ("filter", "min_body_chars", "int", 50,
     "Item body under this many chars is dropped as too_short (unless title ≥ 20 chars or a KB/CVE hit).", "Filter"),
    ("fetching", "default_engagement_threshold", "int", 5,
     "Upvote/comment threshold. Items below this are dropped as low_engagement. "
     "Support-forum sources like Microsoft Community typically have very low engagement — set this to 0 or 1 if you want to keep them.", "Filter"),
    ("filter", "relevance_drop_confidence", "float", 0.7,
     "Only drop an item when the relevance LLM says 'not relevant' AND its confidence ≥ this.", "Filter"),
    ("grouping", "simhash_hamming_threshold", "int", 4,
     "Titles within this Hamming distance are treated as duplicates and dropped as duplicate_title.", "Filter"),
    # --- Fetching (per-source caps) ---
    ("fetching", "new_limit", "int", 1000,
     "Max items from a source's 'new' stream per run.", "Fetching"),
    ("fetching", "top_limit", "int", 100,
     "Max items from 'top' stream per run.", "Fetching"),
    ("fetching", "controversial_limit", "int", 50,
     "Max items from 'controversial' stream per run.", "Fetching"),
    ("fetching", "max_comments_per_post", "int", 500,
     "Safety cap on comments fetched per post.", "Fetching"),
    ("fetching", "parent_context_body_chars", "int", 500,
     "How much of a parent post's body is included as context when classifying its comments.", "Fetching"),
    ("fetching", "sleep_between_streams_seconds", "int", 2,
     "Politeness delay between source streams.", "Fetching"),
    ("fetching", "triangulate", "bool", True,
     "Fetch new + top + controversial and union them (else just 'new').", "Fetching"),
    ("fetching", "fetch_all_comments", "bool", True,
     "Skip engagement gating for comments (fetch every one under a kept post).", "Fetching"),
    # --- Grouping / Scoring / Reporting ---
    ("grouping", "feature_implicated_min_confidence", "float", 0.5,
     "Below this, a mentioned entity is demoted from 'implicated' to a weaker role.", "Grouping"),
    ("scoring", "recency_halflife_days", "int", 7,
     "Recency decay half-life for the item scoring formula.", "Scoring"),
    ("reporting", "trend_weeks", "int", 4,
     "How many weeks of history to show in the trend section of reports.", "Reporting"),
    ("reporting", "top_items_per_area", "int", 10,
     "Max items surfaced per area in the report.", "Reporting"),
    ("reporting", "top_groups_per_area", "int", 10,
     "Max groups surfaced per area in the report.", "Reporting"),
]


def _load_app_yaml_raw() -> dict:
    """Fresh (uncached) read of config/app.yaml. app_config() is lru_cached
    and we may have just written to disk."""
    with _APP_YAML.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _current_tuning_values() -> dict:
    """Flat map {(section, key): current_value} for the form."""
    cfg = _load_app_yaml_raw()
    out: dict[tuple[str, str], object] = {}
    for section, key, _t, default, *_ in _TUNING_FIELDS:
        section_dict = cfg.get(section) or {}
        out[(section, key)] = section_dict.get(key, default)
    return out


def _grouped_fields() -> list[tuple[str, list[dict]]]:
    """Return [(group_label, [field_dict, ...])] in _TUNING_FIELDS order."""
    values = _current_tuning_values()
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for section, key, typ, default, help_text, group in _TUNING_FIELDS:
        if group not in groups:
            groups[group] = []
            order.append(group)
        groups[group].append({
            "section": section,
            "key": key,
            "name": f"{section}.{key}",
            "type": typ,
            "value": values[(section, key)],
            "default": default,
            "help": help_text,
        })
    return [(g, groups[g]) for g in order]


def _parse_tuning_form(form: dict) -> tuple[dict, list[str]]:
    """Convert form -> {section: {key: value}}. Returns (updates, errors)."""
    updates: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    for section, key, typ, default, help_text, _g in _TUNING_FIELDS:
        name = f"{section}.{key}"
        raw = form.get(name)
        if typ == "bool":
            val = raw == "on"
        else:
            if raw is None or str(raw).strip() == "":
                errors.append(f"{name}: value is required")
                continue
            try:
                val = int(raw) if typ == "int" else float(raw)
            except ValueError:
                errors.append(f"{name}: expected {typ}, got {raw!r}")
                continue
            if val < 0:
                errors.append(f"{name}: must be ≥ 0")
                continue
        updates.setdefault(section, {})[key] = val
    return updates, errors


@app.get("/admin/tuning", response_class=HTMLResponse)
def admin_tuning(request: Request, saved: int = 0, error: Optional[str] = None):
    return templates.TemplateResponse(
        "admin_tuning.html",
        {
            "request": request,
            "grouped_fields": _grouped_fields(),
            "yaml_path": str(_APP_YAML),
            "saved": bool(saved),
            "error": error,
        },
    )


@app.post("/admin/tuning")
async def admin_tuning_save(request: Request):
    form = dict(await request.form())
    updates, errors = _parse_tuning_form(form)
    if errors:
        msg = " · ".join(errors)[:300]
        return RedirectResponse(url=f"/admin/tuning?error={msg}", status_code=303)

    # Load current YAML, apply the delta, atomic-write, invalidate caches.
    cfg = _load_app_yaml_raw()
    for section, section_updates in updates.items():
        cfg.setdefault(section, {}).update(section_updates)

    tmp = _APP_YAML.with_suffix(_APP_YAML.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False), encoding="utf-8")
    backup = _APP_YAML.with_suffix(_APP_YAML.suffix + ".bak")
    if _APP_YAML.exists():
        _APP_YAML.replace(backup)
    tmp.replace(_APP_YAML)

    # Invalidate the pipeline's lru_cache on app_config so the next run reads
    # the new values.
    from pipeline.config import app_config as _app_cfg
    _app_cfg.cache_clear()
    clear_cache()

    return RedirectResponse(url="/admin/tuning?saved=1", status_code=303)


# --- Admin: feature flags (POST_V1_PLAN §4.13, ADR-0006) --------------------
#
# UI to flip feature flags in config/features.yaml without opening the file.
# Product-level overrides in products/<pid>/features.yaml still work but
# aren't editable here (edit per-product features.yaml directly for those).


_PHASE_ORDER = {
    "trust_plugins_dir": "Phase 1 (foundation)",
    "assistant_llm_enabled": "Phase 2 (LLM contract)",
    "token_monitor_enabled": "Phase 2 (LLM contract)",
    "prompt_caching_enabled": "Phase 2 (LLM contract)",
    "scrapecreators_enabled": "Phase 3 (external sources)",
    "evals_enabled": "Phase 4 (learning loop)",
    "snippet_candidates_enabled": "Phase 4 (learning loop)",
    "snippet_from_review_enabled": "Phase 4 (learning loop)",
    "wizard_enabled": "Phase 5 (guided experience)",
    "prompt_suggestions_enabled": "Phase 5 (guided experience)",
    "rationale_enabled": "Phase 5 (guided experience)",
    "observability_traces_enabled": "Cross-cutting",
}


_FEATURES_YAML = Path(__file__).resolve().parent.parent / "config" / "features.yaml"


@app.get("/admin/features", response_class=HTMLResponse)
def admin_features(request: Request, saved: int = 0, error: Optional[str] = None):
    """List every declared flag with its current global value + phase label."""
    from pipeline import features as _features

    all_flags = _features.all_flags()
    grouped: dict[str, list[dict]] = {}
    for name, value in sorted(all_flags.items()):
        phase = _PHASE_ORDER.get(name, "Uncategorized")
        grouped.setdefault(phase, []).append({"name": name, "value": bool(value)})

    return templates.TemplateResponse(
        "admin_features.html",
        {
            "request": request,
            "grouped": grouped,
            "yaml_path": str(_FEATURES_YAML),
            "saved": bool(saved),
            "error": error,
        },
    )


@app.post("/admin/features")
async def admin_features_save(request: Request):
    """Save the flag matrix by rewriting config/features.yaml atomically.

    All known flag names are read from the form. Any that appear in the
    form as "on" become true; missing = false (HTML checkboxes only submit
    when checked).
    """
    form = dict(await request.form())
    from pipeline import features as _features

    known_flags = list(_features.all_flags().keys())
    new_map = {flag: (form.get(flag) == "on") for flag in known_flags}

    try:
        current = yaml.safe_load(_FEATURES_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        current = {}
    current["features"] = new_map

    tmp = _FEATURES_YAML.with_suffix(_FEATURES_YAML.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(current, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    if _FEATURES_YAML.exists():
        backup = _FEATURES_YAML.with_suffix(_FEATURES_YAML.suffix + ".bak")
        _FEATURES_YAML.replace(backup)
    tmp.replace(_FEATURES_YAML)

    _features.clear_cache()
    return RedirectResponse(url="/admin/features?saved=1", status_code=303)


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

    from pipeline import connections as _conn
    globally_paused = _conn.paused_types()
    sources_summary = []
    for src in p.sources:
        streams = src.get("streams", []) or []
        stype = src.get("type") or ""
        product_paused = bool(src.get("paused"))
        connection_paused = stype in globally_paused
        # Distinguish which pause is in effect so the user knows where to fix.
        # Precedence in fetch matches this label order: connection > product.
        if connection_paused and product_paused:
            paused_label = "paused (both)"
        elif connection_paused:
            paused_label = "paused (global)"
        elif product_paused:
            paused_label = "paused (product)"
        else:
            paused_label = ""
        sources_summary.append({
            "id": src.get("id"),
            "type": stype,
            "n_streams": len(streams),
            "paused": bool(paused_label),
            "paused_label": paused_label,
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
    tr = p.time_range or {"mode": "incremental"}
    return templates.TemplateResponse(
        "product_meta_form.html",
        {
            "request": request,
            "product": {
                "id": p.id,
                "display": p.display,
                "description": p.description,
                "schedule": p.product_meta.get("schedule") or "weekly",
                "time_range_mode": tr.get("mode") or "incremental",
                "time_range_from": tr.get("range_from") or "",
                "time_range_to": tr.get("range_to") or "",
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
    time_range_mode: str = Form("incremental"),
    time_range_from: str = Form(""),
    time_range_to: str = Form(""),
):
    _product_or_404(product_id)
    display = display.strip()
    if not display:
        return RedirectResponse(
            url=f"/products/{product_id}/edit/meta?error=Display+name+is+required",
            status_code=303,
        )
    if time_range_mode not in ("incremental", "last_week", "last_month", "range"):
        time_range_mode = "incremental"
    if time_range_mode == "range" and (not time_range_from or not time_range_to):
        return RedirectResponse(
            url=f"/products/{product_id}/edit/meta?error=Range+mode+needs+both+from+and+to+dates",
            status_code=303,
        )
    time_range = {"mode": time_range_mode}
    if time_range_mode == "range":
        time_range["range_from"] = time_range_from
        time_range["range_to"] = time_range_to
    try:
        save_product_meta(product_id, display, description, schedule, time_range=time_range)
    except Exception as e:
        return RedirectResponse(
            url=f"/products/{product_id}/edit/meta?error={str(e)[:120]}",
            status_code=303,
        )
    return RedirectResponse(url=f"/products/{product_id}", status_code=303)


# --- Vendors form (Phase 8) -------------------------------------------------
#
# Vendor + product seed list used by the regex pre-pass for entity extraction
# and as hints in the classifier prompt. Each vendor has a canonical name,
# zero or more aliases (alt spellings the regex should also catch), zero or
# more types (the entity types this vendor is associated with), and an
# `active` flag. Stored as products/<id>/vendors.yaml.


@app.get("/products/{product_id}/vendors", response_class=HTMLResponse)
def vendors_form(request: Request, product_id: str):
    product = _product_or_404(product_id)
    vendors = (product.vendors or {}).get("vendors") or []
    types = (product.vendors or {}).get("types") or []
    return templates.TemplateResponse(
        "vendors_form.html",
        {
            "request": request,
            "product": product,
            "vendors": vendors,
            "known_types": types,
            "version": product.vendors_version,
        },
    )


@app.post("/products/{product_id}/vendors")
def vendors_save(product_id: str, payload: dict = Body(...)):
    product_dir = _product_dir_for(product_id)
    vendors_in = payload.get("vendors") or []
    types_in = payload.get("types")  # optional — keep existing if absent

    def _csv(raw: Any) -> list[str]:
        if isinstance(raw, list):
            return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
        if isinstance(raw, str):
            return [s.strip() for s in raw.split(",") if s.strip()]
        return []

    errors: list[str] = []
    seen: set[str] = set()
    cleaned: list[dict] = []
    for vi, v in enumerate(vendors_in):
        canonical = (v.get("canonical") or "").strip()
        if not canonical:
            errors.append(f"vendor #{vi+1}: canonical name is required")
            continue
        key = canonical.lower()
        if key in seen:
            errors.append(f"vendor '{canonical}': duplicate canonical name")
            continue
        seen.add(key)
        cleaned.append({
            "canonical": canonical,
            "aliases": _csv(v.get("aliases")),
            "types": _csv(v.get("types")),
            "products": _csv(v.get("products")),
            "active": bool(v.get("active", True)),
        })

    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    new_doc: dict[str, Any] = {"version": date.today().isoformat()}
    if types_in is not None:
        new_doc["types"] = _csv(types_in)
    else:
        existing_types = (load_product(product_id).vendors or {}).get("types")
        if existing_types:
            new_doc["types"] = existing_types
    new_doc["vendors"] = cleaned

    vendors_path = product_dir / "vendors.yaml"
    backup_path = vendors_path.with_suffix(".yaml.bak")
    if vendors_path.exists():
        vendors_path.replace(backup_path)
    try:
        vendors_path.write_text(
            yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        clear_cache()
        load_product(product_id)
    except Exception as e:
        if vendors_path.exists():
            vendors_path.unlink()
        if backup_path.exists():
            backup_path.replace(vendors_path)
        clear_cache()
        raise HTTPException(status_code=422, detail={"errors": [str(e)]})
    if backup_path.exists():
        backup_path.unlink()
    return {"ok": True, "count": len(cleaned)}


# --- LLM routing form (Phase 8) ---------------------------------------------
#
# Per-stage LLM adapter config: which endpoint, which model, what
# temperature, etc. Each product owns its own routing so different products
# can target different providers.

LLM_PRESETS = [
    {"label": "Foundry Local — Phi-4-mini (Windows local)",
     "endpoint": "http://localhost:5273/v1", "model": "phi-4-mini",
     "note": "Local Foundry Local install. Requires phi-4-mini downloaded via Foundry."},
    {"label": "Ollama — Phi-4-mini (cross-platform local)",
     "endpoint": "http://localhost:11434/v1", "model": "phi4-mini",
     "note": "Local Ollama install (Mac / Linux / Windows). `ollama pull phi4-mini` first."},
    {"label": "Anthropic — Claude Haiku 4.5 (hosted)",
     "endpoint": "https://api.anthropic.com/v1", "model": "claude-haiku-4-5-20251001",
     "note": "Requires ANTHROPIC_API_KEY in .env. Best fit for the cheap relevance stage."},
    {"label": "Anthropic — Claude Sonnet 4.6 (hosted)",
     "endpoint": "https://api.anthropic.com/v1", "model": "claude-sonnet-4-6",
     "note": "Requires ANTHROPIC_API_KEY in .env. Recommended for classify."},
    {"label": "OpenAI — gpt-4o-mini (hosted)",
     "endpoint": "https://api.openai.com/v1", "model": "gpt-4o-mini",
     "note": "Requires OPENAI_API_KEY in .env."},
    {"label": "OpenAI — gpt-4o (hosted)",
     "endpoint": "https://api.openai.com/v1", "model": "gpt-4o",
     "note": "Requires OPENAI_API_KEY in .env."},
]


@app.get("/products/{product_id}/llm_routing", response_class=HTMLResponse)
def llm_routing_form(request: Request, product_id: str, saved: Optional[str] = None, error: Optional[str] = None):
    product = _product_or_404(product_id)
    routing = product.llm_routing or {}
    # Pre-detect provider per stage from the saved endpoint so the form
    # can highlight the right option in the provider dropdown.
    detected = {
        stage: _detect_provider((routing.get(stage) or {}).get("endpoint", ""))
        for stage in ("relevance", "classify")
    }
    return templates.TemplateResponse(
        "llm_routing_form.html",
        {
            "request": request,
            "product": product,
            "routing": routing,
            "providers": _llm_providers_for_template(),
            "detected_provider": detected,
            "saved": saved,
            "error": error,
        },
    )


@app.post("/products/{product_id}/llm_routing")
async def llm_routing_save(product_id: str, request: Request):
    product_dir = _product_dir_for(product_id)
    form = await request.form()

    def _num(name: str, default, kind):
        v = form.get(name)
        if v is None or str(v).strip() == "":
            return default
        try:
            return kind(v)
        except (TypeError, ValueError):
            return default

    def _resolve_endpoint(stage: str) -> str:
        """Derive endpoint from the provider dropdown selection. For
        the 'custom' option, use whatever the user typed in the
        endpoint-override field."""
        provider = (form.get(f"{stage}.provider") or "").strip()
        if provider == "custom":
            return (form.get(f"{stage}.endpoint") or "").strip()
        meta = CONNECTION_META.get(provider) or {}
        if meta.get("category") == "llm":
            return meta.get("api_endpoint") or ""
        # Fallback: provider unknown, honor the (possibly hidden) endpoint field
        return (form.get(f"{stage}.endpoint") or "").strip()

    def _resolve_model(stage: str) -> str:
        """The Model field is now a <select>. The sentinel value
        '__custom__' means "the user wants a model id not in the
        provider's recommended list" — read it from the side text input."""
        m = (form.get(f"{stage}.model") or "").strip()
        if m == "__custom__":
            return (form.get(f"{stage}.model_custom") or "").strip()
        return m

    new_doc = {
        "relevance": {
            "endpoint": _resolve_endpoint("relevance"),
            "model": _resolve_model("relevance"),
            "temperature": _num("relevance.temperature", 0, float),
            "seed": _num("relevance.seed", 42, int),
            "timeout_seconds": _num("relevance.timeout_seconds", 20, int),
            "max_retries": _num("relevance.max_retries", 3, int),
        },
        "classify": {
            "endpoint": _resolve_endpoint("classify"),
            "model": _resolve_model("classify"),
            "temperature": _num("classify.temperature", 0, float),
            "seed": _num("classify.seed", 42, int),
            "timeout_seconds": _num("classify.timeout_seconds", 60, int),
            "max_retries": _num("classify.max_retries", 3, int),
            "use_guided_decoding": form.get("classify.use_guided_decoding") == "on",
            "fallback_repair_attempts": _num("classify.fallback_repair_attempts", 1, int),
        },
    }

    errors = []
    for stage in ("relevance", "classify"):
        if not new_doc[stage]["endpoint"]:
            errors.append(f"{stage}.endpoint is required")
        if not new_doc[stage]["model"]:
            errors.append(f"{stage}.model is required")
    if errors:
        return RedirectResponse(
            url=f"/products/{product_id}/llm_routing?error=" + " | ".join(errors)[:300],
            status_code=303,
        )

    routing_path = product_dir / "llm_routing.yaml"
    backup_path = routing_path.with_suffix(".yaml.bak")
    if routing_path.exists():
        routing_path.replace(backup_path)
    try:
        routing_path.write_text(
            yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        clear_cache()
        load_product(product_id)
    except Exception as e:
        if routing_path.exists():
            routing_path.unlink()
        if backup_path.exists():
            backup_path.replace(routing_path)
        clear_cache()
        return RedirectResponse(
            url=f"/products/{product_id}/llm_routing?error={str(e)[:200]}",
            status_code=303,
        )
    if backup_path.exists():
        backup_path.unlink()
    return RedirectResponse(url=f"/products/{product_id}/llm_routing?saved=1", status_code=303)


# --- Connections (per-source-type connection params, global) ----------------
#
# Connections are credentials / endpoint settings that belong to a source
# TYPE, not to a per-product source instance. They live in the project-root
# .env file (which python-dotenv reads at process start). The connection
# editor reads + writes that file in place via dotenv.set_key, preserving
# any unrelated keys + comments.

ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"

# --- LLM provider metadata (unchanged from V1) ------------------------------
# Sources' connection metadata now comes from each plugin's MANIFEST
# (POST_V1_PLAN §4.1, ADR-0001). LLM providers keep their hardcoded entries
# below until LLMAdapterManifest lands in a follow-on plan.
_LLM_CONNECTION_META: dict[str, dict] = {
    # LLM entries below (skipped for now, filled from the original dict)
}


# --- Source metadata now comes from plugin manifests ------------------------
# The rest of the dict below is left intact for the LLM entries; the source
# entries (reddit through youtube_comments) become unused values that we
# override at the bottom of this section.
_LEGACY_INLINE_META_KEPT_FOR_LLM: dict[str, dict] = {
    # --- Sources (superseded by SourceManifest — see registry-driven build below) ---
    "reddit": {
        "category": "source",
        "display": "Reddit",
        "url": "https://www.reddit.com",
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
        "category": "source",
        "display": "GitHub Issues",
        "url": "https://github.com",
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
        "category": "source",
        "display": "Hacker News",
        "url": "https://news.ycombinator.com",
        "help": (
            "Algolia-hosted HN search index. No authentication required and no "
            "rate-limit ceiling for fair-use traffic. Nothing to configure here."
        ),
        "fields": [],
    },
    "microsoft_community": {
        "category": "source",
        "display": "Microsoft Tech Community (RSS)",
        "url": "https://techcommunity.microsoft.com",
        "help": (
            "Public RSS feeds. No authentication required. Nothing to configure "
            "here. Verify your feed URLs in each product's Sources page."
        ),
        "fields": [],
    },
    "stackex": {
        "category": "source",
        "display": "Stack Exchange",
        "url": "https://api.stackexchange.com/docs",
        "help": (
            "Public 2.3 REST API across Super User, Stack Overflow, Server Fault, "
            "etc. Anonymous mode allows 300 requests/day. Register an app at "
            "stackapps.com/apps/oauth/register (no approval wait, seconds to get "
            "a key) to raise it to 10,000/day."
        ),
        "fields": [
            {"env": "STACKEX_KEY", "label": "API key (optional)", "type": "secret", "default": "",
             "help": "Raises daily quota from 300 to 10,000 requests. Register at stackapps.com/apps/oauth/register — no approval wait."},
        ],
    },
    "apple_appstore": {
        "category": "source",
        "display": "Apple App Store",
        "url": "https://apps.apple.com",
        "help": (
            "Customer reviews via the public iTunes RSS/JSON feed. No auth "
            "required. Reviews are per country per app: ~500 most-recent "
            "reviews are available per country. Configure one stream per app; "
            "add multiple countries to a single stream if you want regional coverage."
        ),
        "fields": [],
    },
    "producthunt": {
        "category": "source",
        "display": "Product Hunt",
        "url": "https://api.producthunt.com/v2/docs",
        "help": (
            "GraphQL v2 API. Register an app at api.producthunt.com/v2/oauth/applications "
            "and click 'Create Token' on the app's page to get a bearer token that "
            "never expires. Rate limit: 900 complexity points / 15 minutes — plenty "
            "for typical usage."
        ),
        "fields": [
            {"env": "PRODUCTHUNT_TOKEN", "label": "Bearer token", "type": "secret", "default": "",
             "help": "Personal developer token from api.producthunt.com/v2/oauth/applications. Required."},
        ],
    },
    "rss": {
        "category": "source",
        "display": "Reddit RSS",
        "url": "https://www.reddit.com",
        "help": (
            "Reddit's per-subreddit RSS feed (also works with any other public "
            "RSS/Atom URL — news sites, blogs, Substack, Beehiiv). Zero auth. "
            "Best used as a Reddit fallback when the OAuth Data API isn't set "
            "up: paste one subreddit URL per stream, e.g. "
            "https://www.reddit.com/r/Windows11/new.rss. The connector "
            "auto-cleans Reddit's HTML wrapper and uses your REDDIT_USER_AGENT "
            "from .env (if set) to avoid rate limits."
        ),
        "fields": [],
    },
    "youtube_comments": {
        # (superseded — see MANIFEST in sources/youtube_comments.py)
        "category": "source",
    },
}

# LLM providers: not yet manifest-driven — hardcoded until LLMAdapterManifest
# lands. These entries are what's actually used for LLM connections.
_LLM_CONNECTION_META = {
    "anthropic": {
        "category": "llm",
        "display": "Anthropic Claude",
        "url": "https://console.anthropic.com",
        "api_endpoint": "https://api.anthropic.com/v1",
        "endpoint_hints": ["anthropic"],
        "help": (
            "Anthropic Claude via the OpenAI-compatible /v1 endpoint. Create a "
            "key at https://console.anthropic.com/account/keys. Recommended "
            "model pairing: Haiku 4.5 for relevance, Sonnet 4.6 (or Opus 4.7) "
            "for classify. Endpoint: https://api.anthropic.com/v1"
        ),
        "fields": [
            {"env": "ANTHROPIC_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Starts with 'sk-ant-…'. Treated as a credential."},
        ],
        "recommended_models": [
            {"id": "claude-haiku-4-5-20251001",
             "purpose": "relevance — cheap, fast"},
            {"id": "claude-sonnet-4-6",
             "purpose": "classify — balanced cost / quality (recommended default)"},
            {"id": "claude-opus-4-7",
             "purpose": "classify — highest quality, more expensive"},
        ],
    },
    "openai": {
        "category": "llm",
        "display": "OpenAI",
        "url": "https://platform.openai.com",
        "api_endpoint": "https://api.openai.com/v1",
        "endpoint_hints": ["openai.com"],
        "help": (
            "OpenAI ChatGPT models. Create a key at "
            "https://platform.openai.com/api-keys. Recommended pairing: "
            "gpt-4o-mini for relevance, gpt-4o (or gpt-4.1) for classify. "
            "Endpoint: https://api.openai.com/v1"
        ),
        "fields": [
            {"env": "OPENAI_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Starts with 'sk-…' or 'sk-proj-…'. Treated as a credential."},
        ],
        "recommended_models": [
            {"id": "gpt-4o-mini",
             "purpose": "relevance — cheap, fast"},
            {"id": "gpt-4o",
             "purpose": "classify — balanced default"},
            {"id": "gpt-4.1",
             "purpose": "classify — newer, stronger"},
            {"id": "o1-mini",
             "purpose": "classify — reasoning model (slow, expensive; overkill for most cases)"},
        ],
    },
    "google_gemini": {
        "category": "llm",
        "display": "Google Gemini",
        "url": "https://ai.google.dev",
        "api_endpoint": "https://generativelanguage.googleapis.com/v1beta/openai",
        "endpoint_hints": ["googleapis.com", "gemini"],
        "help": (
            "Google Gemini via the OpenAI-compatible /v1beta/openai endpoint. "
            "Get a free key at https://aistudio.google.com/app/apikey. "
            "Recommended pairing: Flash for relevance, Pro for classify. "
            "Endpoint: https://generativelanguage.googleapis.com/v1beta/openai"
        ),
        "fields": [
            {"env": "GOOGLE_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Google AI Studio key. Treated as a credential."},
        ],
        "recommended_models": [
            {"id": "gemini-2.0-flash",
             "purpose": "relevance — cheap, fast"},
            {"id": "gemini-2.0-pro",
             "purpose": "classify — balanced default"},
            {"id": "gemini-1.5-flash",
             "purpose": "relevance — fallback if 2.0 unavailable"},
        ],
    },
    "ollama": {
        "category": "llm",
        "display": "Ollama (local)",
        "url": "https://ollama.com",
        "api_endpoint": "http://localhost:11434/v1",
        "endpoint_hints": ["11434", "ollama"],
        "help": (
            "Local cross-platform LLM runtime. No credentials needed — just "
            "have `ollama serve` running and the model pulled "
            "(`ollama pull phi4-mini`). Default endpoint: "
            "http://localhost:11434/v1. Override the URL only if you've "
            "remapped Ollama's port or are pointing at a remote Ollama host."
        ),
        "fields": [
            {"env": "OLLAMA_BASE_URL", "label": "Base URL override (optional)", "type": "text",
             "default": "http://localhost:11434/v1",
             "help": "Leave empty to use the LLM-routing endpoint as-is. Set to override globally."},
        ],
        "recommended_models": [
            {"id": "phi4-mini",  "purpose": "relevance + classify — small (~2 GB)"},
            {"id": "phi3",       "purpose": "relevance + classify — small (~2 GB)"},
            {"id": "llama3.2",   "purpose": "classify — capable mid-size"},
            {"id": "qwen2.5",    "purpose": "classify — strong on instruction following"},
            {"id": "mistral",    "purpose": "classify — popular (~4 GB)"},
        ],
        "model_note": (
            "Ollama models must be pulled first: `ollama pull <id>`. The "
            "Connections page lists what's already pulled on this machine."
        ),
    },
    "foundry_local": {
        "category": "llm",
        "display": "Foundry Local (Windows local)",
        "url": "https://learn.microsoft.com/en-us/azure/ai-studio/foundry-local/",
        "api_endpoint": "http://localhost:5273/v1",
        "endpoint_hints": ["5273", "foundry"],
        "help": (
            "Microsoft Foundry Local. Windows-only. Default endpoint: "
            "http://localhost:5273/v1. No credentials. Override the URL only "
            "if you've remapped the port."
        ),
        "fields": [
            {"env": "FOUNDRY_BASE_URL", "label": "Base URL override (optional)", "type": "text",
             "default": "http://localhost:5273/v1",
             "help": "Leave empty to use the LLM-routing endpoint as-is."},
        ],
        "recommended_models": [
            {"id": "phi-4-mini", "purpose": "relevance + classify — Microsoft's small model"},
            {"id": "phi-4",      "purpose": "classify — larger Phi-4"},
        ],
    },
}


# --- Registry-driven CONNECTION_META and SOURCE_TYPE_META -------------------
# Source metadata now lives in each plugin's MANIFEST (POST_V1_PLAN §4.1,
# ADR-0001). Templates and route handlers still consume the same dict shapes,
# so we build those from the registry at module import.


def _manifest_to_connection_meta(manifest) -> dict:
    """Convert a SourceManifest → the CONNECTION_META entry shape templates expect."""
    return {
        "category": "source" if manifest.category == "source" else manifest.category,
        "display": manifest.display_name,
        "url": manifest.docs_url or "",
        "help": manifest.help,
        "fields": [
            {
                "env": f.name,
                "label": f.label,
                "type": f.type,
                "default": f.default if f.default is not None else "",
                "help": f.help,
            }
            for f in manifest.connection_fields
        ],
    }


def _manifest_to_source_type_meta(manifest) -> dict:
    """Convert a SourceManifest → the SOURCE_TYPE_META entry shape templates expect."""
    return {
        "display": manifest.display_name,
        "help": manifest.help,
        "stream_fields": [
            {
                "name": f.name,
                "label": f.label,
                "type": f.type,
                "required": f.required,
                "default": f.default if f.default is not None else "",
                "placeholder": f.placeholder or "",
                "help": f.help,
            }
            for f in manifest.stream_fields
        ],
    }


_ASSISTANT_LLM_CONNECTION_META = {
    "assistant_llm": {
        "category": "assistant_llm",
        "display": "Assistant LLM (global)",
        "url": "",
        "help": (
            "Global LLM used by the guided setup wizard, snippet candidate "
            "discovery, and prompt suggestions. Distinct from per-product "
            "routing so the wizard has an LLM before per-product config "
            "exists. Configured once at /connections/assistant_llm."
        ),
        "fields": [],   # dedicated form, not env-var-only
    },
}


def _build_meta_dicts() -> tuple[dict[str, dict], dict[str, dict]]:
    """Compute CONNECTION_META and SOURCE_TYPE_META from registered plugins.

    CONNECTION_META = LLM providers (still hardcoded) + source plugins (from registry).
    SOURCE_TYPE_META = source plugins only (LLM providers don't have stream fields).
    """
    from sources.registry import get_registry

    conn_meta: dict[str, dict] = dict(_LLM_CONNECTION_META)
    conn_meta.update(_ASSISTANT_LLM_CONNECTION_META)
    src_type_meta: dict[str, dict] = {}
    for plugin in get_registry().all_plugins():
        m = plugin.manifest
        conn_meta[m.plugin_id] = _manifest_to_connection_meta(m)
        src_type_meta[m.plugin_id] = _manifest_to_source_type_meta(m)
    return conn_meta, src_type_meta


# Computed once at import. Registry is a singleton; if a plugin is added to
# the plugins/ dir at runtime, restart the webui to pick it up.
CONNECTION_META, SOURCE_TYPE_META = _build_meta_dicts()


def _llm_providers_for_template() -> list[dict]:
    """Compact provider list for the LLM routing form's JS, used to map
    a free-text endpoint to its provider and surface recommended models."""
    out = []
    for type_id, meta in CONNECTION_META.items():
        if meta.get("category") != "llm":
            continue
        out.append({
            "type": type_id,
            "display": meta.get("display") or type_id,
            "api_endpoint": meta.get("api_endpoint") or "",
            "endpoint_hints": meta.get("endpoint_hints") or [],
            "models": [
                {"id": m["id"], "purpose": m.get("purpose", "")}
                for m in (meta.get("recommended_models") or [])
            ],
        })
    return out


def _detect_provider(endpoint: str) -> str:
    """Return the provider type id whose endpoint_hints match `endpoint`,
    or 'custom' if none match. Mirrors the client-side _matchProvider."""
    if not endpoint:
        return "custom"
    low = endpoint.lower()
    for type_id, meta in CONNECTION_META.items():
        if meta.get("category") != "llm":
            continue
        for hint in (meta.get("endpoint_hints") or []):
            if hint and hint.lower() in low:
                return type_id
    return "custom"


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
    from pipeline import connections as _conn

    available_sources = set(available_source_types())
    globally_paused = _conn.paused_types()

    def _row(type_id: str, meta: dict) -> dict:
        return {
            "type": type_id,
            "display": meta.get("display") or type_id,
            "url": meta.get("url") or "",
            "n_fields": len(meta.get("fields") or []),
            "status": _connection_status(type_id, env),
            "paused": type_id in globally_paused,
            # Only source-type rows get a pause toggle; LLM providers don't.
            "can_pause": meta.get("category") == "source",
        }

    source_rows: list[dict] = []
    llm_rows: list[dict] = []
    for type_id, meta in CONNECTION_META.items():
        cat = meta.get("category")
        if cat == "source":
            # Only show source types that are also registered as plugins
            # (so the page doesn't list types this build can't actually use).
            if type_id in available_sources:
                source_rows.append(_row(type_id, meta))
        elif cat == "llm":
            llm_rows.append(_row(type_id, meta))

    source_rows.sort(key=lambda r: r["display"])
    llm_rows.sort(key=lambda r: r["display"])

    return templates.TemplateResponse(
        "connections_index.html",
        {
            "request": request,
            "source_rows": source_rows,
            "llm_rows": llm_rows,
            "env_file": str(ENV_FILE_PATH),
        },
    )


# POST_V1_PLAN §4.8 — dedicated assistant LLM form (must register BEFORE
# the generic /connections/{type_id} route so FastAPI matches this first).


@app.get("/connections/assistant_llm", response_class=HTMLResponse)
def assistant_llm_form(request: Request, saved: int = 0, error: Optional[str] = None):
    """Dedicated form for the global assistant LLM (POST_V1_PLAN §4.8)."""
    from pipeline import assistant_llm as _al

    cfg = _al.current_config()
    return templates.TemplateResponse(
        "assistant_llm_form.html",
        {
            "request": request,
            "cfg": cfg,
            "configured": _al.is_configured(),
            "llm_providers": _llm_providers_for_template(),
            "saved": bool(saved),
            "error": error,
        },
    )


@app.post("/connections/assistant_llm")
async def assistant_llm_save(request: Request):
    from pipeline import assistant_llm as _al

    form = dict(await request.form())
    try:
        cfg = _al.AssistantLLMConfig(
            endpoint=form.get("endpoint", "").strip(),
            model=form.get("model", "").strip(),
            temperature=float(form.get("temperature", "0.2") or 0.2),
            seed=int(form["seed"]) if form.get("seed", "").strip() else None,
            timeout_seconds=int(form.get("timeout_seconds", "60") or 60),
            max_retries=int(form.get("max_retries", "3") or 3),
            budget_usd_per_product_per_month=float(
                form.get("budget_usd_per_product_per_month", "10.0") or 10.0
            ),
        )
        if not cfg.endpoint or not cfg.model:
            raise ValueError("endpoint and model are required")
    except (ValueError, KeyError) as e:
        return RedirectResponse(
            url=f"/connections/assistant_llm?error={str(e)[:200]}",
            status_code=303,
        )

    _al.save_config(cfg)
    return RedirectResponse(url="/connections/assistant_llm?saved=1", status_code=303)


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


# --- Ollama lifecycle API ---------------------------------------------------


@app.post("/api/ollama/ensure-running")
def api_ollama_ensure_running(payload: dict = Body(default={})):
    """Detect Ollama, spawn `ollama serve` if needed, return readiness +
    pulled-models list. Called from the connections/ollama page and from
    the LLM routing form on save."""
    from pipeline import ollama_lifecycle
    base_url = (payload or {}).get("base_url") or "http://localhost:11434"
    required_model = (payload or {}).get("required_model") or None
    return ollama_lifecycle.ensure_running(base_url=base_url, required_model=required_model)


@app.post("/api/ollama/pull")
def api_ollama_pull(payload: dict = Body(default={})):
    """Stream Ollama's POST /api/pull progress back as Server-Sent Events.

    The UI calls this when the user clicks "Pull model now" in the LLM
    routing save dialog. Each Ollama JSON line is emitted as one SSE
    `data:` frame so the browser can show a progress bar.
    """
    import json as _json
    from pipeline import ollama_lifecycle
    name = ((payload or {}).get("name") or "").strip()
    base_url = (payload or {}).get("base_url") or "http://localhost:11434"

    def _events():
        for evt in ollama_lifecycle.pull_model(name, base_url=base_url):
            yield f"data: {_json.dumps(evt)}\n\n"

    return StreamingResponse(_events(), media_type="text/event-stream")


@app.post("/api/ollama/install")
def api_ollama_install(payload: dict = Body(default={})):
    """Install Ollama via the official upstream installer for this platform.
    Idempotent — returns ok=true with a message if already installed.

    On Windows: downloads + runs OllamaSetup.exe /SILENT (per-user install,
    no UAC). On macOS / Linux: downloads + pipes install.sh into sh.

    Synchronous: may take 30-120 seconds. The UI button polls this and
    surfaces the status payload inline.
    """
    from pipeline import ollama_lifecycle
    return ollama_lifecycle.install_ollama()


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


@app.post("/connections/{type_id}/pause")
def connection_toggle_pause(type_id: str, paused: str = Form(...)):
    """Set the global pause state for a source type.

    Called from the /connections index page's per-row pause form. `paused`
    is 'true' or 'false' (string form value). Only source-type connections
    can be paused — LLM providers are always active. Precedence: the global
    pause here overrides any product-level `paused: false`.
    """
    if type_id not in CONNECTION_META:
        raise HTTPException(status_code=404, detail=f"unknown connection type: {type_id}")
    if CONNECTION_META[type_id].get("category") != "source":
        raise HTTPException(status_code=400, detail="only source connections can be paused")
    from pipeline import connections as _conn
    _conn.set_paused(type_id, paused.lower() in ("true", "1", "on", "yes"))
    return RedirectResponse(url="/connections", status_code=303)


# --- Sources form (Phase 5) -------------------------------------------------
#
# Per-type form layout so a non-YAML user can add / remove source instances
# and their streams. Type-specific stream fields come from each plugin's
# MANIFEST (POST_V1_PLAN §4.1); SOURCE_TYPE_META is now built above from the
# registry (see _build_meta_dicts).

_LEGACY_SOURCE_TYPE_META_UNUSED: dict[str, dict] = {
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
    "apple_appstore": {
        "display": "Apple App Store",
        "help": (
            "Customer reviews via the public iTunes RSS/JSON feed. No auth. "
            "One stream per app you want to monitor; add multiple countries "
            "to the same stream to get regional coverage. Filter by rating "
            "if you only care about complaints (1-2 stars) vs. all reviews."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "netflix-us", "help": "Internal label for cursor / dedup."},
            {"name": "app_id", "label": "Apple app id", "type": "text", "required": True,
             "placeholder": "363590051",
             "help": "The numeric id from the App Store URL (apps.apple.com/us/app/…/id{THIS}). Copy just the digits."},
            {"name": "countries", "label": "Countries (comma list)", "type": "csv", "required": False, "default": "us",
             "help": "ISO country codes: us, gb, ca, de, fr, jp, kr, in, ... Each is a separate ~500-review pool."},
            {"name": "max_pages", "label": "Max pages per country", "type": "number", "required": False, "default": 10,
             "help": "Apple caps at 10 pages (~500 reviews). Lower this if you only care about the latest N reviews."},
            {"name": "min_rating", "label": "Minimum rating", "type": "number", "required": False, "default": 0,
             "help": "0 = keep all. Set to 3 to drop 3-5 star reviews (keep only complaints)."},
            {"name": "max_rating", "label": "Maximum rating", "type": "number", "required": False, "default": 5,
             "help": "5 = keep all. Set to 2 for a 1-2 star rants-only stream."},
        ],
    },
    "youtube_comments": {
        "display": "YouTube Comments",
        "help": (
            "YouTube Data API v3, search-first flow. Each stream runs one or "
            "more keyword searches, then fetches comments (and full replies) "
            "on the returned videos. Quota-heavy — one search = 100 units, "
            "one comment page = 1 unit. Requires YOUTUBE_API_KEY in .env."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "windows-audio-search", "help": "Internal label for cursor / dedup."},
            {"name": "search_queries", "label": "Search queries (one per line)", "type": "textarea_list", "required": True,
             "placeholder": "windows 11 audio problems\nbluetooth headphones windows\nrealtek driver",
             "help": "One search per line. Each burns 100 units of your daily YouTube quota."},
            {"name": "max_videos_per_query", "label": "Max videos per query", "type": "number", "required": False, "default": 25,
             "help": "Cap on videos discovered per query. YouTube search returns up to 50 per call; lower cap saves comment-fetch quota."},
            {"name": "max_comments_per_video", "label": "Max comments per video", "type": "number", "required": False, "default": 200,
             "help": "Safety cap on hot threads (flagship reviews can have 50K+ comments). Higher = more signal but more quota."},
            {"name": "max_replies_per_thread", "label": "Max replies per thread", "type": "number", "required": False, "default": 100,
             "help": "commentThreads inlines 5 replies for free; this caps how many more we fetch via comments.list (1 unit per page)."},
            {"name": "search_order", "label": "Search order", "type": "text", "required": False, "default": "relevance",
             "help": "relevance | date. 'relevance' surfaces higher-quality videos; 'date' gets the newest."},
            {"name": "comment_order", "label": "Comment order", "type": "text", "required": False, "default": "relevance",
             "help": "relevance | time. 'relevance' surfaces highest-quality comments (YouTube's own ranking)."},
            {"name": "min_video_views", "label": "Min video views", "type": "number", "required": False, "default": 1000,
             "help": "Skip videos below this view count. Filters out obscure/low-engagement content."},
            {"name": "published_within_days", "label": "Only videos from last N days", "type": "number", "required": False, "default": 90,
             "help": "0 = no filter. Recommended: 90-180 for recency; longer wastes quota on stale videos."},
        ],
    },
    "rss": {
        "display": "Reddit RSS",
        "help": (
            "Reddit per-subreddit RSS feeds — the fallback when the OAuth "
            "Data API isn't configured. Paste one subreddit's RSS URL per "
            "stream, e.g. https://www.reddit.com/r/Windows11/new.rss. "
            "For multiple subreddits, set 'Sleep before fetch' to 10+ "
            "seconds each to avoid 429 rate limits, and set REDDIT_USER_AGENT "
            "in .env for a friendlier UA. "
            "The connector also accepts any public RSS/Atom URL (news sites, "
            "blogs, Substack, Beehiiv), so you can use it as a context layer too."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "windows-central", "help": "Internal label for cursor / dedup. Also the default display name."},
            {"name": "feed_url", "label": "Feed URL", "type": "text", "required": True,
             "placeholder": "https://www.windowscentral.com/rss.xml",
             "help": "Public RSS or Atom feed URL. For Reddit: https://www.reddit.com/r/SUBREDDIT/new.rss"},
            {"name": "display", "label": "Display label", "type": "text", "required": False, "default": "",
             "placeholder": "Windows Central",
             "help": "Human-readable name shown in reports. Defaults to the stream name."},
            {"name": "sleep_before_fetch_seconds", "label": "Sleep before fetch (seconds)", "type": "number", "required": False, "default": 0,
             "help": "Pause before this stream fetches. Useful when multiple streams target the same rate-limited host (Reddit: try 3-5)."},
        ],
    },
    "producthunt": {
        "display": "Product Hunt",
        "help": (
            "GraphQL v2. Requires PRODUCTHUNT_TOKEN in .env — get one at "
            "api.producthunt.com/v2/oauth/applications (Create Token). "
            "Streams are topic-filtered. Comments are the substantive "
            "feedback; the post body is mostly launch marketing copy."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "technology-launches", "help": "Internal label for cursor / dedup."},
            {"name": "topic_slug", "label": "Topic slug", "type": "text", "required": False, "default": "",
             "placeholder": "artificial-intelligence",
             "help": "Topic slug from producthunt.com/topics/{slug}. Empty = across all topics (usually too broad)."},
            {"name": "max_posts", "label": "Max posts per run", "type": "number", "required": False, "default": 50,
             "help": "Cap to keep API complexity budget reasonable. Each post also fetches its comments if enabled."},
            {"name": "fetch_comments", "label": "Fetch comments", "type": "bool", "required": False, "default": True,
             "help": "Emit each post's comments as child items. Comments are the substantive feedback."},
            {"name": "max_comments_per_post", "label": "Max comments per post", "type": "number", "required": False, "default": 50,
             "help": "Safety cap on hot threads. Older comments past the cap are skipped."},
        ],
    },
    "stackex": {
        "display": "Stack Exchange",
        "help": (
            "Stack Exchange 2.3 REST across Super User, Stack Overflow, and "
            "sibling sites. Optional STACKEX_KEY in .env raises the daily quota "
            "from 300 to 10K. Each stream is one (site, tags) pair; add a "
            "second stream for a second site. Unanswered questions with high "
            "views are the highest-signal slice — enable 'Unanswered only' for that."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "superuser-windows", "help": "Internal label for cursor / dedup."},
            {"name": "site", "label": "Site", "type": "text", "required": True,
             "placeholder": "superuser",
             "help": "Site slug: superuser | stackoverflow | serverfault | apple | unix | askubuntu | gaming | electronics."},
            {"name": "tags", "label": "Tags (comma or newline list)", "type": "csv", "required": False, "default": "",
             "help": "Tags are AND-joined at the API layer. Empty = all tags on that site (usually too broad — set at least one)."},
            {"name": "unanswered_only", "label": "Unanswered only", "type": "bool", "required": False, "default": False,
             "help": "Only fetch questions without an accepted answer. Highest signal for 'real unresolved pain.'"},
            {"name": "hydrate_answers", "label": "Also fetch answers", "type": "bool", "required": False, "default": False,
             "help": "Fetch answers for each kept question as child items. ~2x quota cost. Off by default."},
            {"name": "max_pages", "label": "Max pages per stream", "type": "number", "required": False, "default": 5,
             "help": "SE returns 100 items/page. Cap keeps a single stream from exhausting the daily quota."},
            {"name": "engagement_threshold", "label": "Engagement threshold", "type": "number", "required": False, "default": 0,
             "help": "Minimum (score + answer_count) to keep a question. 0 = no gate; the pipeline's filter stage handles the rest."},
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


# --- Flat-streams view for the redesigned Sources page ----------------------
#
# The underlying sources.yaml groups streams by source instance:
#   sources: [{id, type, paused, streams: [{...}, {...}]}]
# but the new UI presents a flat list — one row per stream — so users don't
# have to think about instance grouping. These helpers convert between shapes.


# For each source type, which stream-field is the "identifier" (the thing
# users think of as "the subreddit" or "the feed URL"). Used to build the
# preview shown in the flat table's Identifier column.
_TYPE_IDENTIFIER_FIELD: dict[str, str] = {
    "reddit":              "subreddit",
    "hn":                  "search_queries",  # list; take first for label
    "github_issues":       "repos",           # list
    "microsoft_community": "feed_url",
    "stackex":             "tags",            # list
    "apple_appstore":      "app_id",
    "producthunt":         "topic_slug",
    "rss":                 "feed_url",
    "youtube_comments":    "search_queries",  # list
}


def _stream_identifier(stream_type: str, stream: dict) -> str:
    """Short human-readable label for the Identifier column."""
    key = _TYPE_IDENTIFIER_FIELD.get(stream_type)
    if not key:
        return stream.get("name") or "—"
    v = stream.get(key)
    if isinstance(v, list):
        if not v:
            return "—"
        first = v[0]
        if len(v) == 1:
            return str(first)
        return f"{first} +{len(v) - 1} more"
    if v is None or v == "":
        return stream.get("name") or "—"
    if stream_type == "reddit":
        return f"r/{v}"
    if stream_type == "apple_appstore":
        countries = stream.get("countries") or ["us"]
        if isinstance(countries, list):
            countries = ",".join(countries[:3])
        return f"id={v} ({countries})"
    return str(v)


def _flat_streams(sources: list[dict], globally_paused: set[str]) -> list[dict]:
    """Flatten the sources list into per-stream rows, preserving enough info
    that a save can re-group them back into source instances."""
    rows: list[dict] = []
    for src in sources:
        stype = src.get("type") or ""
        instance_id = src.get("id") or ""
        instance_paused = bool(src.get("paused"))
        conn_paused = stype in globally_paused
        for si, stream in enumerate(src.get("streams") or []):
            stream_paused = bool(stream.get("paused"))
            # Effective status label — matches fetch.py precedence.
            if conn_paused:
                status = "paused (connection)"
            elif instance_paused:
                status = "paused (source)"
            elif stream_paused:
                status = "paused (stream)"
            else:
                status = "active"
            rows.append({
                "instance_id": instance_id,
                "stream_index": si,
                "type": stype,
                "identifier": _stream_identifier(stype, stream),
                "display": stream.get("display") or stream.get("name") or "",
                "status": status,
                "paused": stream_paused,           # per-stream pause (what the row toggles)
                "instance_paused": instance_paused, # for the "why is this paused" tooltip
                "connection_paused": conn_paused,
                "stream_data": stream,             # full dict for the Edit modal
            })
    return rows


@app.get("/products/{product_id}/sources", response_class=HTMLResponse)
def sources_form(request: Request, product_id: str):
    product = _product_or_404(product_id)
    from sources import available_source_types
    from pipeline import connections as _conn

    available = available_source_types()
    # Only offer types we have plugin AND metadata for.
    offerable = [t for t in available if t in SOURCE_TYPE_META]
    globally_paused = _conn.paused_types()

    flat = _flat_streams(product.sources, globally_paused)
    return templates.TemplateResponse(
        "sources_form.html",
        {
            "request": request,
            "product": product,
            "sources": product.sources,
            "flat_streams": flat,
            "type_meta": SOURCE_TYPE_META,
            "offerable_types": offerable,
            "identifier_fields": _TYPE_IDENTIFIER_FIELD,
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
        ("{features}", "Hierarchical block: for each area, the features under it with each feature's description (the LLM-recognition prompt you wrote in the taxonomy editor). Use this to give the classifier the per-feature definitions you authored. Truncated to ~200 chars per feature so a 25-feature product stays under ~6KB."),
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


def _project_python() -> str:
    """Pick the Python interpreter for spawned pipeline runs.

    Prefer the project's .venv (which has structlog, duckdb, praw, etc.)
    over `sys.executable` — the webui may be running under a different
    Python (e.g. system Anaconda) that doesn't have the pipeline deps.
    """
    root = Path(__file__).resolve().parent.parent
    candidates = [
        root / ".venv" / "Scripts" / "python.exe",   # Windows venv
        root / ".venv" / "bin" / "python",           # POSIX venv
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def _out_looks_crashed(out_text: str) -> bool:
    """Heuristic: the subprocess wrote a Python traceback / fatal error."""
    if not out_text:
        return False
    tail = out_text[-1200:]
    return ("Traceback (most recent call last)" in tail
            or "ModuleNotFoundError" in tail
            or tail.rstrip().endswith("Error"))


def _list_runs(product_id: str) -> list[dict]:
    """Combine completed .json run logs + still-running .running markers
    + orphan crashed runs (have .out but no .json and no .running)."""
    logs_dir = _run_logs_dir(product_id)
    rows: dict[str, dict] = {}
    if not logs_dir.exists():
        return []

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
                "crashed": False,
            }
        except Exception:
            continue

    for mk in logs_dir.glob("*.running"):
        rid = mk.stem
        rows.setdefault(rid, {
            "run_id": rid, "week_id": None, "status": "running",
            "stage_durations": {}, "counters": {}, "running": True, "crashed": False,
        })

    # Orphan crashed: .out exists but neither .json nor .running.
    for of in logs_dir.glob("*.out"):
        rid = of.stem
        if rid in rows:
            continue
        try:
            tail = of.read_text(encoding="utf-8", errors="replace")
        except Exception:
            tail = ""
        if _out_looks_crashed(tail):
            rows[rid] = {
                "run_id": rid, "week_id": None, "status": "crashed",
                "stage_durations": {}, "counters": {}, "running": False, "crashed": True,
            }

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
    source_options = [
        {"id": s.get("id"), "type": s.get("type"),
         "n_streams": len((s.get("streams") or []))}
        for s in product.sources
    ]
    tr = product.time_range or {"mode": "incremental"}
    # POST_V1_PLAN §4.2 — pre-run readiness card.
    from webui.source_health import compute_readiness
    readiness = compute_readiness(product.sources)
    return templates.TemplateResponse(
        "runs_list.html",
        {
            "request": request,
            "product": product,
            "runs": runs,
            "source_options": source_options,
            "time_range_summary": _summarize_time_range(tr),
            "source_readiness": readiness,
        },
    )


def _summarize_time_range(tr: dict) -> str:
    mode = tr.get("mode") or "incremental"
    if mode == "incremental":
        return "incremental (last cursor → now)"
    if mode == "last_week":
        return "last 7 days"
    if mode == "last_month":
        return "last 30 days"
    if mode == "range":
        return f"{tr.get('range_from') or '?'} → {tr.get('range_to') or '?'}"
    return mode


@app.post("/products/{product_id}/runs")
async def runs_create(product_id: str, request: Request):
    _product_or_404(product_id)
    form = await request.form()
    skip_fetch = form.get("skip_fetch")
    skip_llm = form.get("skip_llm")
    # Multi-select of source ids; empty list = all sources (default).
    selected_sources = [v for v in form.getlist("source_ids") if v]
    # Optional per-run time-mode override; if not set, the persisted product
    # time_range is used by the orchestrator.
    time_mode_override = (form.get("time_mode_override") or "").strip()

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
    # Pass the marker id in as --run-id so the pipeline writes its terminal
    # .json under <marker_id>.json, matching our sidecars. Without this the
    # runs list shows two entries per run (marker + auto-generated) because
    # the .running cleanup path in run_detail looks for <marker_id>.json.
    cmd = [
        _project_python(), "-m", "pipeline.run",
        "--product", product_id,
        "--run-id", marker_id,
    ]
    if skip_fetch:
        cmd.append("--skip-fetch")
    if skip_llm:
        cmd.append("--skip-llm")
    if selected_sources:
        cmd.extend(["--source-ids", ",".join(selected_sources)])
    if time_mode_override and time_mode_override != "saved":
        cmd.extend(["--time-mode", time_mode_override])

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


# Stage order matches pipeline.run.main(). Kept in sync manually — small,
# rarely changes. Used by the flow-diagram parser below.
_PIPELINE_STAGES: tuple[str, ...] = (
    "fetch", "normalize", "filter",
    "relevance", "classify", "score", "group", "aggregate", "render",
)

_STAGE_LINE_RE = re.compile(r"stage=(\w+)")
_STAGE_SECONDS_RE = re.compile(r"seconds=([\d.]+)")


def _parse_stage_states(
    stdout: str,
    run_complete: bool,
    payload: Optional[dict] = None,
) -> list[dict]:
    """Scan the pipeline .out for stage_start / stage_done markers and produce
    a per-stage status list, in pipeline order.

    Statuses:
      pending  - not seen yet (only used while the run is still active)
      running  - stage_start seen, stage_done not yet
      done     - stage_done seen
      skipped  - run finished but stage never started (e.g. --skip-llm)
      failed   - fatal error before this stage's stage_done

    Duration is filled in for `done` stages from the `seconds=` field.

    Fallback: when `payload` is present (the run wrote its terminal JSON), we
    also merge in payload.stage_durations. This matters because a UI-triggered
    run's .out is written under the marker id, not the pipeline run-id — so
    viewing the completed run by its pipeline run-id gives us an empty stdout
    and stage_durations is the only source of truth we have.
    """
    states: dict[str, dict] = {
        s: {"stage": s, "status": "pending", "duration_s": None}
        for s in _PIPELINE_STAGES
    }
    llm_skipped_flag = False
    fatal_seen = False

    for line in (stdout or "").splitlines():
        if "stage_start" in line:
            m = _STAGE_LINE_RE.search(line)
            if m and m.group(1) in states:
                states[m.group(1)]["status"] = "running"
        elif "stage_done" in line:
            sm = _STAGE_LINE_RE.search(line)
            if sm and sm.group(1) in states:
                states[sm.group(1)]["status"] = "done"
                dm = _STAGE_SECONDS_RE.search(line)
                if dm:
                    try:
                        states[sm.group(1)]["duration_s"] = float(dm.group(1))
                    except ValueError:
                        pass
        elif "llm_skipped" in line:
            llm_skipped_flag = True
        elif "pipeline_failed" in line:
            fatal_seen = True

    # LLM-skipped explicitly turns the six LLM-gated stages into "skipped".
    if llm_skipped_flag:
        for s in ("relevance", "classify", "score", "group", "aggregate", "render"):
            if states[s]["status"] == "pending":
                states[s]["status"] = "skipped"

    if fatal_seen:
        for s in states.values():
            if s["status"] == "running":
                s["status"] = "failed"

    # Merge in payload.stage_durations: anything the payload knows ran must be
    # "done" even if the stdout parse missed it (e.g. .out written under a
    # different id, log rotated, etc.).
    if payload:
        for stage, secs in (payload.get("stage_durations") or {}).items():
            if stage in states and states[stage]["status"] in ("pending", "running"):
                states[stage]["status"] = "done"
                if states[stage]["duration_s"] is None:
                    try:
                        states[stage]["duration_s"] = float(secs)
                    except (TypeError, ValueError):
                        pass

    # Run has ended (payload written or crash detected). Anything still
    # "pending" means the stage never executed — usually --skip-fetch or the
    # run died so early the log has no stage_start entries.
    if run_complete:
        for s in states.values():
            if s["status"] == "pending":
                s["status"] = "skipped"
            elif s["status"] == "running":
                s["status"] = "failed"

    return [states[s] for s in _PIPELINE_STAGES]


@app.get("/products/{product_id}/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, product_id: str, run_id: str):
    product = _product_or_404(product_id)
    payload = _read_run(product_id, run_id)
    running = _run_is_running(product_id, run_id) and payload is None
    stdout = _run_stdout(product_id, run_id)
    report_dir = _report_dir_for_run(product_id, payload)

    crashed = False
    # Best-effort marker cleanup.
    marker = _run_logs_dir(product_id) / f"{run_id}.running"
    if payload is not None:
        # JSON exists => run completed normally.
        marker.unlink(missing_ok=True)
    elif running and _out_looks_crashed(stdout):
        # Subprocess wrote a Traceback and stopped — it's not coming back.
        # Clean up the marker so future visits show it as crashed, not stuck.
        marker.unlink(missing_ok=True)
        crashed = True
        running = False

    stage_states = _parse_stage_states(
        stdout, run_complete=(payload is not None or crashed), payload=payload,
    )
    captured = _captured_stages(product_id, run_id)
    source_flow = _per_source_counts(product_id, run_id, captured)
    # Fold per-stage totals into stage_states so the pill can show "stage N".
    for s in stage_states:
        s["total"] = source_flow["totals"].get(s["stage"])
    # POST_V1_PLAN §4.2 — post-run per-source health card. Only compute when
    # the run has finished (payload exists) since compute_health reads errors[].
    source_health_list = []
    if payload is not None:
        from webui.source_health import compute_health
        source_health_list = compute_health(payload, product.sources)

    # POST_V1_PLAN §4.11 — token usage card. Computed for any run with
    # llm_usage rows; gracefully handles empty table.
    from pipeline import features as _features, token_usage as _tu
    token_totals: dict = {}
    if _features.enabled("token_monitor_enabled", product_id):
        token_totals = _tu.per_run_totals(product_id, run_id)
        # Add cost estimates per model
        if token_totals.get("total_tokens", 0) > 0:
            token_totals["estimated_cost_usd"] = _tu.estimate_cost_usd(
                model=(payload or {}).get("model", "") or "unknown",
                prompt_tokens=token_totals.get("prompt_tokens", 0),
                completion_tokens=token_totals.get("completion_tokens", 0),
                cached_input_tokens=token_totals.get("cached_input_tokens", 0),
            )
    return templates.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "payload": payload,
            "running": running,
            "crashed": crashed,
            "stdout_tail": stdout[-4000:] if stdout else "",
            "report_week": (payload or {}).get("week_id") if report_dir else None,
            "captured_stages": captured,
            "stage_states": stage_states,
            "source_flow": source_flow,
            "source_health": source_health_list,
            "token_totals": token_totals,
        },
    )


# --- Trace viewer (POST_V1_PLAN §4.16) --------------------------------------
#
# Renders the JSONL span file at
# data/<pid>/temp_runs/<run_id>/trace.jsonl as a waterfall.
# Read-only. Trace file is written by pipeline/tracing.py during the run.


@app.get("/products/{product_id}/runs/{run_id}/trace", response_class=HTMLResponse)
def run_trace_view(request: Request, product_id: str, run_id: str):
    product = _product_or_404(product_id)
    trace_path = _temp_run_dir(product_id, run_id) / "trace.jsonl"
    spans: list[dict] = []
    if trace_path.exists():
        try:
            with trace_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        spans.append(_json.loads(line))
                    except Exception:
                        continue
        except Exception:
            spans = []

    # Compute a normalized waterfall — bar left offset + width as %
    if spans:
        # Sort by start_ts; earliest first
        spans.sort(key=lambda s: s.get("start_ts", 0))
        t_min = spans[0].get("start_ts", 0)
        t_max = max(s.get("end_ts", 0) for s in spans)
        total = max(t_max - t_min, 0.001)
        for s in spans:
            left = ((s.get("start_ts", t_min) - t_min) / total) * 100
            width = max(((s.get("end_ts", t_min) - s.get("start_ts", t_min)) / total) * 100, 0.5)
            s["_left_pct"] = round(left, 3)
            s["_width_pct"] = round(width, 3)

    return templates.TemplateResponse(
        "run_trace.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "spans": spans,
            "trace_path": str(trace_path),
        },
    )


# --- Per-stage snapshots (temp_runs) ----------------------------------------
#
# After each pipeline stage, pipeline.stage_capture writes a JSONL of the
# joined item view + a meta.json to data/<pid>/temp_runs/<run_id>/. These
# routes browse those files. Snapshots are kept forever; a delete button on
# the run detail page wipes just that run's temp dir.


def _temp_runs_root(product_id: str) -> Path:
    return _product_data_root(product_id) / "temp_runs"


def _temp_run_dir(product_id: str, run_id: str) -> Path:
    return _temp_runs_root(product_id) / run_id


def _per_source_counts(product_id: str, run_id: str, captured: list[dict]) -> dict:
    """Walk each captured stage's .jsonl and compute per-source "still in-flight"
    counts. Returns:

        {
          "sources": [(source_id, display_name), ...],   # union across stages
          "by_stage": {stage: {source_id: kept_count}},  # kept per source
          "totals":   {stage: total_kept},               # summed across sources
        }

    "Kept" for warehouse stages = filter_status in (None, 'passed') AND
    is_relevant in (None, True). For the fetch stage snapshot (which lists
    raw JSONL files, not warehouse rows), kept = sum(line_count) per source.
    """
    d = _temp_run_dir(product_id, run_id)
    by_stage: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    displays: dict[str, str] = {}

    for meta in captured:
        stage = meta["stage"]
        jsonl = d / f"{stage}.jsonl"
        if not jsonl.exists():
            continue
        counts: dict[str, int] = {}
        total = 0
        try:
            with jsonl.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = _json.loads(line)
                    except Exception:
                        continue
                    src = row.get("source") or "unknown"
                    disp = row.get("source_display_name") or src
                    displays.setdefault(src, disp)
                    if stage == "fetch":
                        n = int(row.get("line_count") or 0)
                        counts[src] = counts.get(src, 0) + n
                        total += n
                    else:
                        fs = row.get("filter_status")
                        ir = row.get("is_relevant")
                        kept = fs in (None, "passed") and (ir is None or ir is True)
                        if kept:
                            counts[src] = counts.get(src, 0) + 1
                            total += 1
        except Exception:
            continue
        by_stage[stage] = counts
        totals[stage] = total

    # Stable source order: highest ever-seen count first, ties by name
    max_by_source: dict[str, int] = {}
    for counts in by_stage.values():
        for src, n in counts.items():
            if n > max_by_source.get(src, 0):
                max_by_source[src] = n
    sources = sorted(max_by_source.keys(), key=lambda s: (-max_by_source[s], s))
    return {
        "sources": [(s, displays.get(s, s)) for s in sources],
        "by_stage": by_stage,
        "totals": totals,
    }


def _captured_stages(product_id: str, run_id: str) -> list[dict]:
    """Return [{stage, row_count, duration_s, has_error}, ...] in capture order."""
    d = _temp_run_dir(product_id, run_id)
    idx_path = d / "stages.json"
    if not idx_path.exists():
        return []
    try:
        idx = _json.loads(idx_path.read_text(encoding="utf-8"))
        stages = idx.get("stages") or []
    except Exception:
        return []
    out: list[dict] = []
    for s in stages:
        meta_path = d / f"{s}.meta.json"
        row_count, duration, err = None, None, False
        if meta_path.exists():
            try:
                m = _json.loads(meta_path.read_text(encoding="utf-8"))
                row_count = m.get("row_count")
                duration = m.get("duration_s")
                err = bool(m.get("capture_error"))
            except Exception:
                pass
        out.append({"stage": s, "row_count": row_count, "duration_s": duration, "has_error": err})
    return out


_STAGE_VIEWS = ("in-flight", "dropped", "all")


def _row_is_in_flight(row: dict) -> bool:
    """Same "kept" definition used by the top-of-page source table:
    filter_status hasn't dropped it AND relevance didn't mark it not-relevant."""
    fs = row.get("filter_status")
    ir = row.get("is_relevant")
    return fs in (None, "passed") and (ir is None or ir is True)


def _read_stage_jsonl(
    path: Path, offset: int, limit: int, view: str = "all",
) -> tuple[list[dict], int, int]:
    """Read a slice of the JSONL, with an optional view filter (in-flight /
    dropped / all).

    Returns (rows_on_this_page, total_matching_view, total_in_file).
    Two counters so the sub-page can say "showing X of Y matching (Z total in warehouse)".

    The JSONL is per-run scale (hundreds of rows) so a full linear scan is fine.
    """
    if not path.exists():
        return [], 0, 0
    matches: list[dict] = []
    total_in_file = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            total_in_file += 1
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except Exception:
                row = {"_parse_error": True, "_raw": line[:200]}
            if view == "in-flight" and not _row_is_in_flight(row):
                continue
            if view == "dropped" and _row_is_in_flight(row):
                continue
            matches.append(row)
    total_matching = len(matches)
    return matches[offset:offset + limit], total_matching, total_in_file


@app.get("/products/{product_id}/runs/{run_id}/stages/{stage}", response_class=HTMLResponse)
def stage_snapshot(
    request: Request,
    product_id: str,
    run_id: str,
    stage: str,
    offset: int = 0,
    limit: int = 50,
    view: str = "in-flight",
):
    product = _product_or_404(product_id)
    d = _temp_run_dir(product_id, run_id)
    jsonl_path = d / f"{stage}.jsonl"
    meta_path = d / f"{stage}.meta.json"
    if not jsonl_path.exists():
        raise HTTPException(status_code=404, detail=f"no snapshot for stage {stage!r}")

    # Fetch stage has no filter_status concept — force 'all'.
    if stage == "fetch" or view not in _STAGE_VIEWS:
        view = "all"

    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows, total, total_in_file = _read_stage_jsonl(jsonl_path, offset, limit, view=view)

    meta: dict = {}
    if meta_path.exists():
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    # Union of keys seen across the current page — stable-ish column order:
    # base identity first, then classification, then everything else alphabetical.
    preferred = [
        "id", "source", "week_id", "created_at", "author", "title", "body",
        "filter_status", "is_relevant", "relevance_score",
        "primary_area", "sentiment", "content_types_json", "summary",
        "severity", "score",
    ]
    keys_seen = {k for r in rows for k in r.keys()}
    ordered = [k for k in preferred if k in keys_seen] + sorted(
        k for k in keys_seen if k not in preferred
    )

    # Kept-count so the sub-page header can show both "in warehouse" (rows in
    # this JSONL) and "in-flight" (items where filter_status is passed/null
    # and is_relevant isn't False) — matches the parent run detail table.
    kept_total = None
    try:
        captured_for_kept = _captured_stages(product_id, run_id)
        source_flow_kept = _per_source_counts(product_id, run_id, captured_for_kept)
        kept_total = source_flow_kept.get("totals", {}).get(stage)
    except Exception:
        pass

    return templates.TemplateResponse(
        "stage_snapshot.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "stage": stage,
            "meta": meta,
            "rows": rows,
            "columns": ordered,
            "total": total,
            "total_in_file": total_in_file,
            "offset": offset,
            "limit": limit,
            "view": view,
            "views_available": _STAGE_VIEWS if stage != "fetch" else ("all",),
            "kept_total": kept_total,
            "all_stages": _captured_stages(product_id, run_id),
        },
    )


@app.get("/products/{product_id}/runs/{run_id}/stages/{stage}/download")
def stage_snapshot_download(product_id: str, run_id: str, stage: str):
    _product_or_404(product_id)
    p = _temp_run_dir(product_id, run_id) / f"{stage}.jsonl"
    if not p.exists():
        raise HTTPException(status_code=404, detail="no snapshot")
    return FileResponse(
        str(p),
        media_type="application/x-ndjson",
        filename=f"{run_id}-{stage}.jsonl",
    )


@app.post("/products/{product_id}/runs/{run_id}/stages/delete")
def stage_snapshots_delete(product_id: str, run_id: str):
    _product_or_404(product_id)
    d = _temp_run_dir(product_id, run_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    return RedirectResponse(url=f"/products/{product_id}/runs/{run_id}", status_code=303)


# --- Per-run config snapshot browser ----------------------------------------
#
# stage_capture.snapshot_config copies the config that produced each run into
# temp_runs/<run_id>/config_snapshot/. These routes browse it so you can answer
# "what settings did this run use?" long after the live config has changed.


def _config_snapshot_dir(product_id: str, run_id: str) -> Path:
    return _temp_run_dir(product_id, run_id) / "config_snapshot"


def _list_config_snapshot_files(product_id: str, run_id: str) -> list[dict]:
    """Return a flat list of files in the snapshot, sorted for stable display.
    Each entry: {relpath, name, group, size_bytes}."""
    root = _config_snapshot_dir(product_id, run_id)
    if not root.exists():
        return []
    entries: list[dict] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        group = "global" if "/" not in rel else rel.split("/", 1)[0]
        entries.append({
            "relpath": rel,
            "name": p.name,
            "group": group,
            "size_bytes": p.stat().st_size,
        })
    return entries


def _safe_snapshot_file(product_id: str, run_id: str, relpath: str) -> Path:
    """Resolve `relpath` inside the snapshot dir, rejecting anything that
    escapes it (path traversal defense)."""
    root = _config_snapshot_dir(product_id, run_id).resolve()
    candidate = (root / relpath).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"file {relpath!r} not in snapshot")
    return candidate


@app.get("/products/{product_id}/runs/{run_id}/config", response_class=HTMLResponse)
def run_config_snapshot(request: Request, product_id: str, run_id: str):
    product = _product_or_404(product_id)
    entries = _list_config_snapshot_files(product_id, run_id)
    runtime = None
    runtime_path = _config_snapshot_dir(product_id, run_id) / "runtime.json"
    if runtime_path.exists():
        try:
            runtime = _json.loads(runtime_path.read_text(encoding="utf-8"))
        except Exception:
            runtime = None
    return templates.TemplateResponse(
        "run_config.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "entries": entries,
            "runtime": runtime,
            "has_snapshot": bool(entries),
        },
    )


@app.get("/products/{product_id}/runs/{run_id}/config/view", response_class=HTMLResponse)
def run_config_snapshot_view(request: Request, product_id: str, run_id: str, relpath: str):
    product = _product_or_404(product_id)
    path = _safe_snapshot_file(product_id, run_id, relpath)
    body = path.read_text(encoding="utf-8", errors="replace")
    return templates.TemplateResponse(
        "run_config_file.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "relpath": relpath,
            "name": path.name,
            "body": body,
            "size_bytes": path.stat().st_size,
        },
    )


@app.get("/products/{product_id}/runs/{run_id}/config/download")
def run_config_snapshot_download(product_id: str, run_id: str, relpath: str):
    _product_or_404(product_id)
    path = _safe_snapshot_file(product_id, run_id, relpath)
    return FileResponse(
        str(path),
        media_type="text/yaml" if path.suffix in (".yaml", ".yml") else "application/octet-stream",
        filename=f"{run_id}-{path.name}",
    )


# --- Per-run post review ----------------------------------------------------
#
# Every item in the run + the reason it was kept or dropped. Reads the latest
# stage snapshot (classify.jsonl if it exists; else the last one written) so
# we see the item's final filter_status / is_relevant after every stage that
# has run so far.

_FILTER_STATUS_HELP = {
    "passed":                  "Kept by filter — text/engagement/dedup all OK.",
    "dropped:too_short":       "Body under min_body_chars AND title < 20 chars AND no KB/CVE watchlist match.",
    "dropped:duplicate_url":   "Canonical URL was already seen earlier in the batch.",
    "dropped:duplicate_title": "Title matches an earlier item's simhash within grouping.simhash_hamming_threshold.",
    "dropped:low_engagement":  "Upvotes AND comments both below fetching.default_engagement_threshold (raise to 0 to keep everything).",
    "dropped:deleted_or_empty":"Body was [deleted] / [removed] / empty AND no title.",
    "dropped:not_topic_relevant": "Relevance LLM decided this isn't on-topic (score above filter.relevance_drop_confidence).",
    "dropped:not_relevant":       "Relevance LLM decided this isn't relevant (older path).",
    "classification_failed":   "Classify stage failed for this item (LLM error or schema violation).",
    "dropped:conditional_violation": "Classify stage's structured output violated a conditional schema constraint.",
}


def _outcome_class(filter_status: str | None, is_relevant) -> str:
    """CSS class hint for the row: 'kept' / 'dropped-filter' / 'dropped-relevance' / 'dropped-classify' / 'inflight'."""
    if not filter_status:
        return "inflight"
    if filter_status == "passed":
        if is_relevant is True:
            return "kept"
        if is_relevant is False:
            return "dropped-relevance"
        return "inflight"
    if "not_relevant" in filter_status or "not_topic_relevant" in filter_status:
        return "dropped-relevance"
    if "classification" in filter_status:
        return "dropped-classify"
    return "dropped-filter"


def _outcome_label(filter_status: str | None, is_relevant) -> str:
    if not filter_status:
        return "not filtered yet"
    if filter_status == "passed":
        if is_relevant is True:
            return "KEPT (relevant)"
        if is_relevant is False:
            return "dropped by relevance"
        return "kept by filter (pending relevance)"
    if filter_status.startswith("dropped:"):
        return f"dropped: {filter_status.split(':', 1)[1]}"
    return filter_status


def _pick_review_snapshot(product_id: str, run_id: str) -> Optional[Path]:
    """Pick the most complete snapshot for the review list.

    Order of preference (each is a *superset* of the last in terms of state
    populated per item):
      classify > relevance > filter > normalize
    Then fall back to whichever stage was last captured.
    """
    d = _temp_run_dir(product_id, run_id)
    for stage in ("classify", "relevance", "filter", "normalize"):
        p = d / f"{stage}.jsonl"
        if p.exists():
            return p
    # last-captured fallback
    idx = d / "stages.json"
    if idx.exists():
        try:
            stages = _json.loads(idx.read_text(encoding="utf-8")).get("stages") or []
            for s in reversed(stages):
                p = d / f"{s}.jsonl"
                if p.exists() and s != "fetch":
                    return p
        except Exception:
            pass
    return None


def _read_review_items(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except Exception:
                continue
            fs = row.get("filter_status")
            ir = row.get("is_relevant")
            row["_outcome_class"] = _outcome_class(fs, ir)
            row["_outcome_label"] = _outcome_label(fs, ir)
            row["_reason_help"] = _FILTER_STATUS_HELP.get(fs or "", "")
            rows.append(row)
    return rows


@app.get("/products/{product_id}/runs/{run_id}/review", response_class=HTMLResponse)
def run_review(
    request: Request,
    product_id: str,
    run_id: str,
    outcome: Optional[str] = None,
    source: Optional[str] = None,
    reason: Optional[str] = None,
    q: Optional[str] = None,
):
    product = _product_or_404(product_id)
    snap = _pick_review_snapshot(product_id, run_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshots for this run")

    items = _read_review_items(snap)

    # Facets before filtering, so dropdowns show the full menu.
    reason_counts: dict[str, int] = {}
    for i in items:
        r = i.get("filter_status") or "(none — pre-filter)"
        reason_counts[r] = reason_counts.get(r, 0) + 1
    facets = {
        "outcomes": sorted({i["_outcome_class"] for i in items}),
        "sources":  sorted({i.get("source") for i in items if i.get("source")}),
        # (label, value, count) — sorted by count desc so common reasons come first
        "reasons":  sorted(
            [(r, r, n) for r, n in reason_counts.items()],
            key=lambda t: (-t[2], t[0]),
        ),
    }
    outcome_counts = {}
    for i in items:
        outcome_counts[i["_outcome_class"]] = outcome_counts.get(i["_outcome_class"], 0) + 1

    # Apply filters.
    filtered = items
    if outcome:
        filtered = [i for i in filtered if i["_outcome_class"] == outcome]
    if source:
        filtered = [i for i in filtered if i.get("source") == source]
    if reason:
        if reason == "(none — pre-filter)":
            filtered = [i for i in filtered if not i.get("filter_status")]
        else:
            filtered = [i for i in filtered if i.get("filter_status") == reason]
    if q:
        needle = q.lower()
        filtered = [i for i in filtered
                    if needle in (i.get("title") or "").lower()
                    or needle in (i.get("body") or "").lower()
                    or needle in (i.get("author") or "").lower()]

    return templates.TemplateResponse(
        "run_review.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "snapshot_stage": snap.stem,
            "items": filtered,
            "total_all": len(items),
            "total_shown": len(filtered),
            "facets": facets,
            "outcome_counts": outcome_counts,
            "filters": {"outcome": outcome or "", "source": source or "", "reason": reason or "", "q": q or ""},
        },
    )


@app.get("/products/{product_id}/runs/{run_id}/review/detail", response_class=HTMLResponse)
def run_review_detail(request: Request, product_id: str, run_id: str, item_id: str):
    product = _product_or_404(product_id)
    snap = _pick_review_snapshot(product_id, run_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshots for this run")

    item = None
    for row in _read_review_items(snap):
        if row.get("id") == item_id:
            item = row
            break
    if item is None:
        raise HTTPException(status_code=404, detail=f"item {item_id!r} not in this run's snapshot")

    # Walk every stage snapshot to build a per-stage state transition list.
    journey: list[dict] = []
    d = _temp_run_dir(product_id, run_id)
    idx_path = d / "stages.json"
    stages: list[str] = []
    if idx_path.exists():
        try:
            stages = _json.loads(idx_path.read_text(encoding="utf-8")).get("stages") or []
        except Exception:
            pass
    prev_fs, prev_ir = "<absent>", "<absent>"
    for stage in stages:
        p = d / f"{stage}.jsonl"
        if not p.exists() or stage == "fetch":
            journey.append({"stage": stage, "state": None, "changed": False, "note": "fetch snapshot is per-file" if stage == "fetch" else "no snapshot"})
            continue
        row = None
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("id") == item_id:
                    row = r
                    break
        if row is None:
            journey.append({"stage": stage, "state": None, "changed": False, "note": "not present"})
            continue
        fs = row.get("filter_status")
        ir = row.get("is_relevant")
        changed = (fs != prev_fs) or (ir != prev_ir)
        journey.append({
            "stage": stage,
            "state": {"filter_status": fs, "is_relevant": ir, "relevance_score": row.get("relevance_score")},
            "changed": changed,
        })
        prev_fs, prev_ir = fs, ir

    return templates.TemplateResponse(
        "run_review_detail.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "item": item,
            "journey": journey,
            "reason_help": _FILTER_STATUS_HELP.get(item.get("filter_status") or "", ""),
        },
    )


# --- Fetched-items browser (debugging) --------------------------------------
#
# Surfaces the per-product DuckDB `items` table so the user can see what
# fetch + normalize produced, plus the optional classification rows. Linked
# from the Runs page. Read-only.


@app.get("/products/{product_id}/items", response_class=HTMLResponse)
def items_list(
    request: Request,
    product_id: str,
    source: Optional[str] = None,
    week: Optional[str] = None,
    relevance: Optional[str] = None,   # 'yes' | 'no' | 'unset'
    offset: int = 0,
):
    from pipeline import storage
    product = _product_or_404(product_id)
    set_current_product(product)
    limit = 50

    ctx_empty = {
        "request": request, "product": product,
        "items": [], "total": 0,
        "filters": {"source": source or "", "week": week or "",
                    "relevance": relevance or "", "offset": 0, "limit": limit},
        "facets": {"sources": [], "weeks": []},
        "no_warehouse": True,
    }
    if not storage.warehouse_path().exists():
        return templates.TemplateResponse("items_list.html", ctx_empty)

    where: list[str] = []
    params: list = []
    if source:
        where.append("source = ?"); params.append(source)
    if week:
        where.append("week_id = ?"); params.append(week)
    if relevance == "yes":
        where.append("is_relevant = TRUE")
    elif relevance == "no":
        where.append("is_relevant = FALSE")
    elif relevance == "unset":
        where.append("is_relevant IS NULL")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    try:
        total = storage.query(f"SELECT COUNT(*) AS n FROM items{where_sql}", params)[0]["n"]
        rows = storage.query(
            "SELECT id, source, source_display_name, week_id, created_at, author, "
            "url, title, body, is_relevant, filter_status, is_reply, author_intent "
            f"FROM items{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        sources = [r["source"] for r in storage.query(
            "SELECT DISTINCT source FROM items ORDER BY source")]
        weeks = [r["week_id"] for r in storage.query(
            "SELECT DISTINCT week_id FROM items ORDER BY week_id DESC")]
    except Exception:
        # Empty / fresh schema, no rows yet.
        ctx_empty["no_warehouse"] = False
        return templates.TemplateResponse("items_list.html", ctx_empty)

    return templates.TemplateResponse("items_list.html", {
        "request": request, "product": product,
        "items": rows, "total": total,
        "filters": {"source": source or "", "week": week or "",
                    "relevance": relevance or "", "offset": offset, "limit": limit},
        "facets": {"sources": sources, "weeks": weeks},
        "no_warehouse": False,
    })


@app.get("/products/{product_id}/items/detail", response_class=HTMLResponse)
def item_detail(request: Request, product_id: str, item_id: str):
    from pipeline import storage
    product = _product_or_404(product_id)
    set_current_product(product)

    rows = storage.query("SELECT * FROM items WHERE id = ?", [item_id])
    if not rows:
        raise HTTPException(status_code=404, detail=f"item {item_id} not found")
    item = rows[0]

    cls_rows = storage.query(
        "SELECT * FROM item_classifications WHERE item_id = ?", [item_id])
    classification = cls_rows[0] if cls_rows else None

    areas = storage.query(
        "SELECT area, is_primary FROM item_areas WHERE item_id = ? ORDER BY is_primary DESC, area",
        [item_id])

    # Try to recover the original fetched record from the raw JSONL it came from.
    raw_json = None
    raw_ref = item.get("raw_ref")
    if raw_ref:
        raw_path = Path(raw_ref)
        if raw_path.exists():
            try:
                import json as _json
                with raw_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        rec = _json.loads(line)
                        if (rec.get("external_id") == item["external_id"]
                                and rec.get("source") == item["source"]):
                            raw_json = rec
                            break
            except Exception:
                pass

    return templates.TemplateResponse("item_detail.html", {
        "request": request, "product": product, "item": item,
        "classification": classification, "areas": areas, "raw_json": raw_json,
    })


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
