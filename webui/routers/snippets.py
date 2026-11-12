"""Labelled snippets: list, edit, fetch, candidates, calibrate.

Split out of webui/app.py (ADR-0026). Shared helpers live in
webui/core.py and are reached as `core.<name>` so a test patch there
reaches every caller.
"""

from __future__ import annotations

import json as _json
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pathlib import Path
from pipeline.models import CONTENT_TYPES, SEVERITY_VALUES
from pipeline.product import load_product
from pipeline.snippets import (
    NEGATIVE,
    POSITIVE,
    Snippet,
    delete_snippet,
    save_snippet,
    slugify,
)
from typing import Optional

from webui import core

router = APIRouter()


def _snippets_index_url(product_id: str) -> str:
    return f"/products/{product_id}/snippets"


@router.get("/products/{product_id}/snippets", response_class=HTMLResponse)
def snippets_list(request: Request, product_id: str):
    product = core._product_or_404(product_id)
    snips = sorted(product.snippets, key=lambda s: (not s.is_positive, s.id))
    return core.templates.TemplateResponse(
        "snippets_list.html",
        {"request": request, "product": product, "snippets": snips},
    )


@router.get("/products/{product_id}/snippets/new", response_class=HTMLResponse)
def snippets_new_form(request: Request, product_id: str, mode: str = "url",
                      error: Optional[str] = None):
    product = core._product_or_404(product_id)
    if mode not in ("url", "text"):
        mode = "url"
    return core.templates.TemplateResponse(
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
            "error": error,
        },
    )


@router.post("/products/{product_id}/snippets/fetch")
async def snippets_fetch_url(product_id: str, request: Request):
    """Resolve title/body for a public URL (warehouse → Reddit/HN → HTML).

    Used by the URL-mode snippet form before Create. No API keys required.
    """
    core._product_or_404(product_id)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    url = (payload.get("url") if isinstance(payload, dict) else "") or ""
    from pipeline.snippet_fetch import fetch_snippet_content
    result = fetch_snippet_content(url, product_id=product_id)
    return {
        "ok": result.ok,
        "url": result.url,
        "title": result.title,
        "body": result.body,
        "source_display_name": result.source_display_name,
        "from_warehouse": result.from_warehouse,
        "error": result.error,
    }


# Route order matters: /snippets/candidates* must be registered BEFORE
# /snippets/{snippet_id} so the parameterised route doesn't swallow it.
# The real handlers are defined further down; here we just forward.

@router.get("/products/{product_id}/snippets/candidates", response_class=HTMLResponse)
def _snippets_candidates_route(
    request: Request,
    product_id: str,
    nonce: Optional[str] = None,
    idx: int = 0,
    error: Optional[str] = None,
):
    return snippets_candidates(request, product_id, nonce, idx, error)


@router.post("/products/{product_id}/snippets/candidates/decide")
async def _snippets_candidates_decide_route(product_id: str, request: Request):
    return await snippets_candidates_decide(product_id, request)


# --- Calibrate-more: reuse wizard mini-fetch on a LIVE product -----------------
#
# Fetch ~50 items from selected sources, judge them relevant / not relevant,
# then flush judgments as snippet YAML files. Reuses pipeline/minifetch.py and
# webui/templates/wizard/step_calibrate.html (with calibrate_more_mode=True).
# State lives at data/.wizard/product-<id>/ so it can't collide with wizard drafts.
#
# Route order: /snippets/calibrate* MUST come before /snippets/{snippet_id}
# so the parameterised GET/POST don't swallow it.


def _product_as_calibrate_draft(product):
    """Minimal draft-shaped object for step_calibrate.html reuse. Only carries
    the fields the template actually reads (slug, display, calibration)."""
    from types import SimpleNamespace
    from pipeline import calibrate_more as _cm
    return SimpleNamespace(
        slug=product.id,
        display=product.display or product.id,
        calibration={"judgments": _cm.read_judgments(product.id)},
    )


def _available_calibrate_sources(product):
    """Sources shown on the calibrate picker — active (non-paused) sources with
    at least one stream. `stream_count` is displayed as a hint.

    product.sources are plain dicts (see pipeline.product.ProductSpec)."""
    out = []
    for src in (product.sources or []):
        if src.get("paused"):
            continue
        streams = src.get("streams") or []
        if not streams:
            continue
        out.append({
            "id": src.get("id"),
            "type": src.get("type"),
            "stream_count": len(streams),
        })
    return out


@router.get("/products/{product_id}/snippets/calibrate", response_class=HTMLResponse)
def snippets_calibrate_page(request: Request, product_id: str):
    from pipeline import calibrate_more as _cm
    from pipeline import minifetch as _mf
    product = core._product_or_404(product_id)
    draft = _product_as_calibrate_draft(product)
    status = _cm.read_status(product_id)
    judged = draft.calibration["judgments"]
    deck = _cm.sample_deck(product_id, size=10) if status.status == _mf.STATUS_READY else []
    return core.templates.TemplateResponse(
        "wizard/step_calibrate.html",
        {
            "request": request,
            "draft": draft,
            "calibrate_more_mode": True,
            "minifetch_status": status,
            "deck": deck,
            "n_judged": len(judged),
            "min_useful": _mf.MIN_USEFUL_ITEMS,
            "available_sources": _available_calibrate_sources(product),
        },
    )


@router.post("/products/{product_id}/snippets/calibrate/start")
async def snippets_calibrate_start(product_id: str, request: Request):
    from pipeline import calibrate_more as _cm
    product = core._product_or_404(product_id)
    form = await request.form()
    selected = form.getlist("source_id")
    use_llm_gate = bool(form.get("use_llm_gate"))
    if not selected:
        # No sources picked and no defaults available — bounce back with a note.
        available = _available_calibrate_sources(product)
        if not available:
            return RedirectResponse(
                url=f"/products/{product_id}/snippets",
                status_code=303,
            )
        # User unchecked everything — treat as 'use all'.
        selected = [s["id"] for s in available]
    _cm.start(product, selected, use_llm_gate=use_llm_gate)
    return RedirectResponse(
        url=f"/products/{product_id}/snippets/calibrate",
        status_code=303,
    )


@router.post("/products/{product_id}/snippets/calibrate")
async def snippets_calibrate_judge(product_id: str, request: Request):
    from pipeline import calibrate_more as _cm
    core._product_or_404(product_id)
    form = await request.form()
    item_id = (form.get("item_id") or "").strip()
    verdict = (form.get("verdict") or "").strip()
    if item_id and verdict in ("relevant", "not_relevant"):
        corpus_item = _cm.find_corpus_item(product_id, item_id) or {}
        _cm.record_judgment(product_id, item_id, verdict, corpus_item)
    # verdict='skip' falls through — no record, next GET drops it from the deck
    # once judged advances (skip stays visible until judged).
    return RedirectResponse(
        url=f"/products/{product_id}/snippets/calibrate",
        status_code=303,
    )


@router.post("/products/{product_id}/snippets/calibrate/done")
def snippets_calibrate_done(product_id: str):
    from pipeline import calibrate_more as _cm
    core._product_or_404(product_id)
    product_dir = core._product_dir_for(product_id)
    n_pos, n_neg = _cm.flush_to_snippets(product_id, product_dir)
    core.clear_cache()
    load_product(product_id)  # refresh in-memory product cache with new snippets
    return RedirectResponse(
        url=f"/products/{product_id}/snippets",
        status_code=303,
    )


@router.post("/products/{product_id}/snippets/calibrate/reset")
def snippets_calibrate_reset(product_id: str):
    from pipeline import calibrate_more as _cm
    core._product_or_404(product_id)
    _cm.reset(product_id)
    return RedirectResponse(
        url=f"/products/{product_id}/snippets/calibrate",
        status_code=303,
    )


@router.get("/products/{product_id}/snippets/{snippet_id}", response_class=HTMLResponse)
def snippets_edit_form(request: Request, product_id: str, snippet_id: str, error: Optional[str] = None):
    product = core._product_or_404(product_id)
    snip = next((s for s in product.snippets if s.id == snippet_id), None)
    if snip is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    # Edit always uses the same simplified URL form as /snippets/new?mode=url.
    return core.templates.TemplateResponse(
        "snippet_form.html",
        {
            "request": request,
            "product": product,
            "mode": "url",
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


@router.post("/products/{product_id}/snippets")
async def snippets_create(product_id: str, request: Request):
    product = core._product_or_404(product_id)
    form = await request.form()
    mode = (form.get("mode") or "url").strip()
    polarity = form.get("polarity") or POSITIVE
    source_url = (form.get("source_url") or "").strip()
    title = (form.get("title") or "").strip()
    body = (form.get("body") or "").strip()

    # URL mode: content must come from a prior fetch (hidden fields). If the
    # operator somehow skipped Fetch, try once server-side before failing.
    if mode == "url" and source_url and not body:
        from pipeline.snippet_fetch import fetch_snippet_content
        fetched = fetch_snippet_content(source_url, product_id=product_id)
        if fetched.ok:
            title = title or fetched.title
            body = fetched.body
        else:
            from urllib.parse import quote
            return RedirectResponse(
                url=(
                    f"/products/{product_id}/snippets/new?mode=url"
                    f"&error={quote(fetched.error or 'Could not fetch URL')}"
                ),
                status_code=303,
            )

    # Simplified create modes: is_topic_relevant defaults from polarity.
    if mode in ("url", "text") and "is_topic_relevant" not in form:
        is_topic_relevant = polarity == POSITIVE
    else:
        is_topic_relevant = form.get("is_topic_relevant") == "on"

    # Text create: coarse Classification → content_types.
    _CLASSIFICATION_ALLOWED = {"bug_report", "feature_request", "feedback"}
    content_types_in = _parse_areas(form.getlist("content_types"))
    if mode == "text":
        classification = (form.get("classification") or "").strip()
        if classification not in _CLASSIFICATION_ALLOWED:
            from urllib.parse import quote
            return RedirectResponse(
                url=(
                    f"/products/{product_id}/snippets/new?mode=text"
                    f"&error={quote('Choose a classification: Bug / Issue, Feature, or General context')}"
                ),
                status_code=303,
            )
        content_types_in = [classification]
        if not title:
            from urllib.parse import quote
            return RedirectResponse(
                url=(
                    f"/products/{product_id}/snippets/new?mode=text"
                    f"&error={quote('Title is required')}"
                ),
                status_code=303,
            )
        if not body:
            from urllib.parse import quote
            return RedirectResponse(
                url=(
                    f"/products/{product_id}/snippets/new?mode=text"
                    f"&error={quote('Body is required')}"
                ),
                status_code=303,
            )

    try:
        if mode == "url" and not body:
            raise ValueError("Fetch the URL first so title/body are filled in")
        snippet = _build_snippet_from_form(
            product=product,
            snippet_id=None,
            polarity=polarity,
            source_url=source_url,
            title=title,
            body=body,
            summary=(form.get("summary") or "").strip(),
            is_topic_relevant=is_topic_relevant,
            areas=_parse_areas(form.getlist("areas")),
            content_types_in=content_types_in,
            sentiment=float(form["sentiment"]) if form.get("sentiment") else None,
            bug_severity=(form.get("bug_severity") or "").strip(),
            notes=form.get("notes") or "",
            holdout_eval=form.get("holdout_eval") == "on",
        )
    except ValueError as e:
        from urllib.parse import quote
        return RedirectResponse(
            url=(
                f"/products/{product_id}/snippets/new?mode={mode}"
                f"&error={quote(str(e)[:200])}"
            ),
            status_code=303,
        )
    product_dir = core.PRODUCTS_DIR / product_id
    save_snippet(product_dir, snippet)
    core.clear_cache()
    return RedirectResponse(url=_snippets_index_url(product_id), status_code=303)


@router.post("/products/{product_id}/snippets/{snippet_id}")
async def snippets_update(product_id: str, snippet_id: str, request: Request):
    product = core._product_or_404(product_id)
    existing = next((s for s in product.snippets if s.id == snippet_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    form = await request.form()
    polarity = form.get("polarity") or existing.polarity
    source_url = (form.get("source_url") or "").strip()
    title = (form.get("title") or "").strip()
    body = (form.get("body") or "").strip()

    # Same simplified URL form as create: re-fetch if body missing but URL given.
    if source_url and not body:
        from pipeline.snippet_fetch import fetch_snippet_content
        fetched = fetch_snippet_content(source_url, product_id=product_id)
        if fetched.ok:
            title = title or fetched.title
            body = fetched.body

    # Preserve labels the simplified form no longer edits; only refresh
    # is_topic_relevant from polarity.
    labels = dict(existing.labels or {})
    labels["is_topic_relevant"] = polarity == POSITIVE

    try:
        if not body and not source_url:
            raise ValueError("snippet needs either a URL or body text")
        if not body:
            raise ValueError("Fetch the URL first so title/body are filled in")
        new_snippet = Snippet(
            id=snippet_id,
            polarity=polarity,
            source_url=source_url or None,
            title=title or None,
            body=body,
            labels=labels,
            holdout_eval=existing.holdout_eval,
            notes=existing.notes or "",
            path=existing.path,
            created_at=existing.created_at,
        )
    except ValueError as e:
        from urllib.parse import quote
        return RedirectResponse(
            url=f"/products/{product_id}/snippets/{snippet_id}?error={quote(str(e)[:120])}",
            status_code=303,
        )

    # Polarity change moves the file across directories — delete the old one
    # (in its old polarity dir) before saving the new one.
    if existing.polarity != new_snippet.polarity:
        delete_snippet(existing)

    product_dir = core.PRODUCTS_DIR / product_id
    save_snippet(product_dir, new_snippet)
    core.clear_cache()
    return RedirectResponse(url=_snippets_index_url(product_id), status_code=303)


@router.post("/products/{product_id}/snippets/{snippet_id}/delete")
def snippets_delete(product_id: str, snippet_id: str):
    product = core._product_or_404(product_id)
    existing = next((s for s in product.snippets if s.id == snippet_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    delete_snippet(existing)
    core.clear_cache()
    return RedirectResponse(url=_snippets_index_url(product_id), status_code=303)


# --- Snippet candidates (POST_V1_PLAN §4.4-B) -------------------------------
#
# LLM-proposed candidates from the warehouse. Feature-flagged by
# `snippet_candidates_enabled`. Pool is generated once per session, cached
# to disk, then walked one-at-a-time with keyboard shortcuts (Y/N/S).


def _candidates_cache_path(product_id: str, nonce: str) -> Path:
    return core._product_data_root(product_id) / "temp_runs" / f"candidates_{nonce}.json"


def _load_candidate_cache(product_id: str, nonce: str) -> Optional[dict]:
    path = _candidates_cache_path(product_id, nonce)
    if not path.exists():
        return None
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def snippets_candidates(
    request: Request,
    product_id: str,
    nonce: Optional[str] = None,
    idx: int = 0,
    error: Optional[str] = None,
):
    from pipeline import features as _features
    product = core._product_or_404(product_id)
    if not _features.enabled("snippet_candidates_enabled", product_id):
        return core.templates.TemplateResponse(
            "snippets_candidates.html",
            {
                "request": request, "product": product,
                "flag_off": True, "cache": None, "error": error,
            },
        )

    # No nonce → fresh pool: sample warehouse, ask assistant LLM, cache to disk.
    if not nonce:
        from pipeline import snippet_candidates as _sc
        pool = _sc.sample_candidate_pool(product_id)
        if not pool.items:
            return core.templates.TemplateResponse(
                "snippets_candidates.html",
                {
                    "request": request, "product": product,
                    "flag_off": False, "cache": None, "error": (
                        "No relevant items in the warehouse yet. Run the pipeline "
                        "to ingest and classify some data first."
                    ),
                },
            )
        zipped = _sc.rank_and_zip(pool)
        if not zipped:
            return core.templates.TemplateResponse(
                "snippets_candidates.html",
                {
                    "request": request, "product": product,
                    "flag_off": False, "cache": None, "error": (
                        "Assistant LLM produced no suggestions. Verify the assistant "
                        "LLM is configured at /connections/assistant_llm and within budget."
                    ),
                },
            )
        nonce = uuid.uuid4().hex[:12]
        cache = {
            "nonce": nonce,
            "product_id": product_id,
            "total_relevant": pool.total_relevant,
            "per_area_counts": pool.per_area_counts,
            "candidates": [
                {"item": z["item"], "suggestion":
                    z["suggestion"].model_dump() if z["suggestion"] else None,
                 "decided": None}
                for z in zipped if z["suggestion"] is not None    # only ranked
            ],
        }
        # Serialize datetimes to ISO before writing.
        for c in cache["candidates"]:
            ca = c["item"].get("created_at")
            if hasattr(ca, "isoformat"):
                c["item"]["created_at"] = ca.isoformat()
        path = _candidates_cache_path(product_id, nonce)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(cache), encoding="utf-8")
        return RedirectResponse(
            url=f"/products/{product_id}/snippets/candidates?nonce={nonce}&idx=0",
            status_code=303,
        )

    cache = _load_candidate_cache(product_id, nonce)
    if not cache:
        return RedirectResponse(
            url=f"/products/{product_id}/snippets/candidates",
            status_code=303,
        )
    total = len(cache.get("candidates") or [])
    idx = max(0, min(idx, total))
    return core.templates.TemplateResponse(
        "snippets_candidates.html",
        {
            "request": request, "product": product, "flag_off": False,
            "cache": cache, "idx": idx, "total": total, "error": error,
        },
    )


async def snippets_candidates_decide(product_id: str, request: Request):
    """Accept or skip a candidate. Creates a snippet with created_at=now
    when accepted (positive_example / negative_example) or just advances
    the cursor when skipped."""
    product = core._product_or_404(product_id)
    form = await request.form()
    nonce = (form.get("nonce") or "").strip()
    try:
        idx = int(form.get("idx") or 0)
    except ValueError:
        idx = 0
    decision = (form.get("decision") or "skip").strip()

    cache = _load_candidate_cache(product_id, nonce)
    if not cache:
        return RedirectResponse(
            url=f"/products/{product_id}/snippets/candidates",
            status_code=303,
        )
    candidates = cache.get("candidates") or []
    if 0 <= idx < len(candidates):
        cand = candidates[idx]
        item = cand.get("item") or {}
        if decision in (POSITIVE, NEGATIVE):
            snippet = Snippet(
                id=slugify((item.get("title") or item.get("body") or "candidate")[:60]),
                polarity=decision,
                source_url=item.get("url") or None,
                title=item.get("title") or None,
                body=item.get("body") or "",
                labels={
                    "is_topic_relevant": (decision == POSITIVE),
                    "areas": [item["primary_area"]] if item.get("primary_area")
                              and item["primary_area"] != "(unknown)" else [],
                    "summary": item.get("summary") or "",
                },
                holdout_eval=False,
                notes=(cand.get("suggestion") or {}).get("why", ""),
                created_at=datetime.now(timezone.utc),
            )
            # Collision handling: append -2 / -3 / ...
            existing_ids = {s.id for s in product.snippets}
            if snippet.id in existing_ids:
                base, n = snippet.id, 2
                while f"{base}-{n}" in existing_ids:
                    n += 1
                snippet.id = f"{base}-{n}"
            try:
                product_dir = core.PRODUCTS_DIR / product_id
                save_snippet(product_dir, snippet)
                core.clear_cache()
                cand["decided"] = decision
            except Exception as e:
                _candidates_cache_path(product_id, nonce).write_text(
                    _json.dumps(cache), encoding="utf-8",
                )
                return RedirectResponse(
                    url=f"/products/{product_id}/snippets/candidates?nonce={nonce}&idx={idx}&error={str(e)[:120]}",
                    status_code=303,
                )
        else:
            cand["decided"] = "skip"

        _candidates_cache_path(product_id, nonce).write_text(
            _json.dumps(cache), encoding="utf-8",
        )

    return RedirectResponse(
        url=f"/products/{product_id}/snippets/candidates?nonce={nonce}&idx={idx + 1}",
        status_code=303,
    )
