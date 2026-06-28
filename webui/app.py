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
