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

from pipeline.topic import available_topics, clear_cache, load_topic, scaffold_topic

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
