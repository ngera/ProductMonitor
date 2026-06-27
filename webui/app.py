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


@app.get("/topics/{topic_id}/edit/{section}", response_class=HTMLResponse)
def yaml_editor(request: Request, topic_id: str, section: str, error: Optional[str] = None):
    if section not in _EDITORS:
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


@app.post("/topics/{topic_id}/edit/{section}")
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
            url=f"/topics/{topic_id}/edit/{section}?error=YAML+parse+error:+{str(e)[:120]}",
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
            url=f"/topics/{topic_id}/edit/{section}?error=Validation+failed:+{msg}",
            status_code=303,
        )

    if backup is not None and backup.exists():
        backup.unlink()
    return RedirectResponse(url=f"/topics/{topic_id}/edit/{section}?error=", status_code=303)



# --- Snippets (UI 3) --------------------------------------------------------


def _topic_or_404(topic_id: str):
    try:
        return load_topic(topic_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


def _snippets_index_url(topic_id: str) -> str:
    return f"/topics/{topic_id}/snippets"


@app.get("/topics/{topic_id}/snippets", response_class=HTMLResponse)
def snippets_list(request: Request, topic_id: str):
    topic = _topic_or_404(topic_id)
    snips = sorted(topic.snippets, key=lambda s: (not s.is_positive, s.id))
    return templates.TemplateResponse(
        "snippets_list.html",
        {"request": request, "topic": topic, "snippets": snips},
    )


@app.get("/topics/{topic_id}/snippets/new", response_class=HTMLResponse)
def snippets_new_form(request: Request, topic_id: str, mode: str = "url"):
    topic = _topic_or_404(topic_id)
    if mode not in ("url", "text"):
        mode = "url"
    return templates.TemplateResponse(
        "snippet_form.html",
        {
            "request": request,
            "topic": topic,
            "mode": mode,
            "snippet": None,                 # new
            "area_ids": topic.area_ids(),
            "content_types": sorted(CONTENT_TYPES),
            "severity_values": ["", *sorted(SEVERITY_VALUES)],
            "form_action": f"/topics/{topic_id}/snippets",
            "edit": False,
            "error": None,
        },
    )


@app.get("/topics/{topic_id}/snippets/{snippet_id}", response_class=HTMLResponse)
def snippets_edit_form(request: Request, topic_id: str, snippet_id: str, error: Optional[str] = None):
    topic = _topic_or_404(topic_id)
    snip = next((s for s in topic.snippets if s.id == snippet_id), None)
    if snip is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    mode = "url" if snip.source_url else "text"
    return templates.TemplateResponse(
        "snippet_form.html",
        {
            "request": request,
            "topic": topic,
            "mode": mode,
            "snippet": snip,
            "area_ids": topic.area_ids(),
            "content_types": sorted(CONTENT_TYPES),
            "severity_values": ["", *sorted(SEVERITY_VALUES)],
            "form_action": f"/topics/{topic_id}/snippets/{snippet_id}",
            "edit": True,
            "error": error,
        },
    )


def _build_snippet_from_form(
    *,
    topic,
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
    existing_ids = {s.id for s in topic.snippets}
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


@app.post("/topics/{topic_id}/snippets")
async def snippets_create(topic_id: str, request: Request):
    topic = _topic_or_404(topic_id)
    form = await request.form()
    try:
        snippet = _build_snippet_from_form(
            topic=topic,
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
            url=f"/topics/{topic_id}/snippets/new?mode={form.get('mode', 'url')}",
            status_code=303,
        )
    topic_dir = TOPICS_DIR / topic_id
    save_snippet(topic_dir, snippet)
    clear_cache()
    return RedirectResponse(url=_snippets_index_url(topic_id), status_code=303)


@app.post("/topics/{topic_id}/snippets/{snippet_id}")
async def snippets_update(topic_id: str, snippet_id: str, request: Request):
    topic = _topic_or_404(topic_id)
    existing = next((s for s in topic.snippets if s.id == snippet_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    form = await request.form()
    try:
        new_snippet = _build_snippet_from_form(
            topic=topic,
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
            url=f"/topics/{topic_id}/snippets/{snippet_id}?error={str(e)[:120]}",
            status_code=303,
        )

    # Polarity change moves the file across directories — delete the old one
    # (in its old polarity dir) before saving the new one.
    if existing.polarity != new_snippet.polarity:
        delete_snippet(existing)

    topic_dir = TOPICS_DIR / topic_id
    save_snippet(topic_dir, new_snippet)
    clear_cache()
    return RedirectResponse(url=_snippets_index_url(topic_id), status_code=303)


@app.post("/topics/{topic_id}/snippets/{snippet_id}/delete")
def snippets_delete(topic_id: str, snippet_id: str):
    topic = _topic_or_404(topic_id)
    existing = next((s for s in topic.snippets if s.id == snippet_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    delete_snippet(existing)
    clear_cache()
    return RedirectResponse(url=_snippets_index_url(topic_id), status_code=303)


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
