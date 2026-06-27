"""FastAPI admin app, bound to 127.0.0.1.

UI 1 — topic list/create + per-topic dashboard skeleton. Subsequent UI
phases add: source editor, prompts editor, taxonomy editor, snippet
add/list/edit, run trigger + report viewer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import yaml

from pipeline.topic import TOPICS_DIR, available_topics, clear_cache, load_topic, scaffold_topic

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"

app = FastAPI(title="Customer Feedback Monitor")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Index: list + create topic ---------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    topics = []
    for tid in available_topics():
        try:
            t = load_topic(tid)
            topics.append({
                "id": t.id,
                "display": t.display,
                "description": t.description,
                "n_sources": len(t.sources),
                "n_areas": len(t.area_ids()),
                "n_snippets": len(t.snippets),
            })
        except Exception as e:
            topics.append({"id": tid, "display": tid, "error": str(e)})
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "topics": topics},
    )


@app.post("/topics")
def create_topic(
    topic_id: str = Form(...),
    display: str = Form(...),
    description: str = Form(""),
):
    topic_id = topic_id.strip().lower().replace(" ", "-")
    display = display.strip()
    if not topic_id or not display:
        raise HTTPException(status_code=400, detail="topic_id and display are required")
    try:
        scaffold_topic(topic_id, display, description.strip())
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=f"/topics/{topic_id}", status_code=303)


# --- Per-topic dashboard ----------------------------------------------------


@app.get("/topics/{topic_id}", response_class=HTMLResponse)
def topic_dashboard(request: Request, topic_id: str):
    try:
        t = load_topic(topic_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    sources_summary = []
    for src in t.sources:
        streams = src.get("streams", []) or []
        sources_summary.append({
            "id": src.get("id"),
            "type": src.get("type"),
            "n_streams": len(streams),
        })

    n_positive = sum(1 for s in t.snippets if s.is_positive)
    n_negative = sum(1 for s in t.snippets if s.is_negative)
    n_holdout = sum(1 for s in t.snippets if s.holdout_eval)

    return templates.TemplateResponse(
        "topic.html",
        {
            "request": request,
            "topic": {
                "id": t.id,
                "display": t.display,
                "description": t.description,
                "extras_class": t.extras_cls.__name__,
                "taxonomy_version": t.taxonomy_version,
                "n_areas": len(t.area_ids()),
                "areas_preview": t.area_ids()[:6],
            },
            "sources_summary": sources_summary,
            "snippet_stats": {
                "total": len(t.snippets),
                "positive": n_positive,
                "negative": n_negative,
                "holdout": n_holdout,
            },
        },
    )


# --- YAML editors (UI 2) ----------------------------------------------------
#
# Each per-topic YAML file (sources.yaml, prompts.yaml, taxonomy.yaml,
# vendors.yaml, llm_routing.yaml) has the same shape of editor:
#
#   GET  /topics/{id}/<thing>          render YAML in a textarea
#   POST /topics/{id}/<thing>          parse + validate (via topic re-load),
#                                      write file on success, redirect back
#
# Validation strategy: write to a temp file, attempt to YAML-parse it, attempt
# to re-load the topic with the new content swapped in (catches schema-level
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
        "help": "Relevance + classify prompt templates. Placeholders: {topic_display}, {title}, {body}, {areas}, {content_types}, {few_shot_block}, {vendor_hits}, {kb_numbers}, {build_numbers}, {parent_block}, {extras_instructions}.",
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


def _topic_dir_for(topic_id: str) -> Path:
    d = TOPICS_DIR / topic_id
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"topic '{topic_id}' not found")
    return d


@app.get("/topics/{topic_id}/{section}", response_class=HTMLResponse)
def yaml_editor(request: Request, topic_id: str, section: str, error: Optional[str] = None):
    if section not in _EDITORS:
        # Not a YAML editor — fall through to whatever else handles this path.
        raise HTTPException(status_code=404, detail=f"unknown section: {section}")
    meta = _EDITORS[section]
    topic_dir = _topic_dir_for(topic_id)
    file_path = topic_dir / meta["filename"]
    body = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    return templates.TemplateResponse(
        "yaml_editor.html",
        {
            "request": request,
            "topic_id": topic_id,
            "section": section,
            "title": meta["title"],
            "filename": meta["filename"],
            "help": meta["help"],
            "body": body,
            "error": error,
        },
    )


@app.post("/topics/{topic_id}/{section}")
def yaml_editor_save(topic_id: str, section: str, body: str = Form(...)):
    if section not in _EDITORS:
        raise HTTPException(status_code=404, detail=f"unknown section: {section}")
    meta = _EDITORS[section]
    topic_dir = _topic_dir_for(topic_id)
    file_path = topic_dir / meta["filename"]

    # 1. Parse YAML — surface syntax errors back to the editor.
    try:
        yaml.safe_load(body)
    except yaml.YAMLError as e:
        return RedirectResponse(
            url=f"/topics/{topic_id}/{section}?error=YAML+parse+error:+{str(e)[:120]}",
            status_code=303,
        )

    # 2. Write atomically (write to tmp, swap).
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")

    # 3. Reload-validate. If load_topic raises, roll back.
    clear_cache()
    backup = None
    if file_path.exists():
        backup = file_path.with_suffix(file_path.suffix + ".bak")
        file_path.replace(backup)
    tmp.replace(file_path)
    try:
        load_topic(topic_id)
    except Exception as e:
        # Roll back.
        file_path.unlink(missing_ok=True)
        if backup is not None:
            backup.replace(file_path)
        clear_cache()
        msg = str(e)[:150].replace("+", " ")
        return RedirectResponse(
            url=f"/topics/{topic_id}/{section}?error=Validation+failed:+{msg}",
            status_code=303,
        )

    if backup is not None and backup.exists():
        backup.unlink()
    return RedirectResponse(url=f"/topics/{topic_id}/{section}?error=", status_code=303)


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
