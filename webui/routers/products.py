"""Product dashboard and core product settings.

Split out of webui/app.py (ADR-0026). Shared helpers live in
webui/core.py and are reached as `core.<name>` so a test patch there
reaches every caller.
"""

from __future__ import annotations

import shutil
import yaml
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pipeline.product import load_product, save_product_facts, save_product_meta
from typing import Optional

from webui import core

router = APIRouter()


# --- Per-product dashboard --------------------------------------------------


@router.get("/products/{product_id}", response_class=HTMLResponse)
def product_dashboard(request: Request, product_id: str,
                      notice: Optional[str] = None, error: Optional[str] = None):
    p = core._product_or_404(product_id)

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

    # Summary tab dashboard (new default tab). Wraps run-log + warehouse
    # queries in one place so the template stays declarative.
    from webui.services.runs import product_dashboard_summary
    from webui.services import dashboard as _dash
    from pipeline import features as _features
    summary = _dash.extend_dashboard_summary(
        product_id, product_dashboard_summary(product_id),
    )

    # Schedule card (Run Setup tab) — moved here from the runs_list route
    # so the tab can show just the configuration, not the past-runs table.
    from pipeline import scheduler as _scheduler
    schedule = _scheduler.load(product_id)
    schedule_flag_on = _features.enabled("scheduler_enabled", product_id)
    next_due = None
    if schedule.enabled:
        try:
            next_due = _scheduler.next_due_at(schedule).isoformat(timespec="minutes")
        except Exception:
            next_due = None

    tr = p.time_range or {"mode": "incremental"}

    return core.templates.TemplateResponse(
        "product.html",
        {
            "request": request,
            "product": {
                "id": p.id,
                "display": p.display,
                "description": p.description,
                "extras_class": (
                    p.extras_cls.__name__
                    if p.product_meta.get("extras_module") else ""
                ),
                "taxonomy_version": p.taxonomy_version,
                "n_areas": len(p.area_ids()),
                "n_features": n_features,
                "areas_preview": p.area_ids()[:6],
                "url": p.url,
                "aliases": p.aliases,
                "not_to_be_confused_with": p.not_to_be_confused_with,
                "goals": p.goals,
                "competitors": p.competitors,
                "scope_in": p.scope_in,
                "scope_out": p.scope_out,
            },
            "sources_summary": sources_summary,
            "snippet_stats": {
                "total": len(p.snippets),
                "positive": n_positive,
                "negative": n_negative,
                "holdout": n_holdout,
            },
            "summary": summary,
            "schedule": schedule,
            "schedule_cadences": _scheduler.CADENCES,
            "schedule_time_windows": _scheduler.TIME_WINDOWS,
            "schedule_flag_on": schedule_flag_on,
            "schedule_next_due": next_due,
            "time_range_summary": core._summarize_time_range(tr),
            "competition_analysis_enabled": _features.enabled(
                "competition_analysis_enabled",
            ),
            "notice": notice,
            "error": error,
        },
    )


# --- Product metadata editor (Phase 4) --------------------------------------


@router.get("/products/{product_id}/edit/meta", response_class=HTMLResponse)
def product_meta_form(request: Request, product_id: str, error: Optional[str] = None):
    p = core._product_or_404(product_id)
    tr = p.time_range or {"mode": "incremental"}
    return core.templates.TemplateResponse(
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


@router.post("/products/{product_id}/edit/meta")
def product_meta_save(
    product_id: str,
    display: str = Form(...),
    description: str = Form(""),
    schedule: str = Form("weekly"),
    time_range_mode: str = Form("incremental"),
    time_range_from: str = Form(""),
    time_range_to: str = Form(""),
):
    core._product_or_404(product_id)
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


# --- Profile page (wizard redesign Phase 6) ---------------------------------
#
# Structured facts editor for a live product. Same chip UI as the wizard's
# Screen 2 (via the shared `wizard/_chips.html` partial). Independent of the
# wizard flag — always available so users can maintain facts after creation.

def _product_as_wizard_draft(product):
    """Adapter — return a SimpleNamespace shaped like WizardV2Draft, populated
    from an existing product. Lets `webui/templates/wizard/step_profile.html`
    (originally built for wizard drafts) also render as the /products/<id>/
    profile edit page. Fields the wizard template touches:
        slug, display, url, description, aliases, not_to_be_confused_with,
        competitors (list of name strings), scope_in, scope_out,
        regenerations (empty), drafting_error, page_fetch_failed, updated_at
    Rich competitor attributes (aliases/color/context) are preserved on the
    ProductSpec side; the wizard chip UI only edits the visible NAME.
    """
    from types import SimpleNamespace
    comp_names = [
        (c.get("name") if isinstance(c, dict) else str(c)) or ""
        for c in (product.competitors or [])
    ]
    return SimpleNamespace(
        slug=product.id,
        display=product.display or product.id,
        description=product.description or "",
        url=getattr(product, "url", "") or "",
        aliases=list(product.aliases or []),
        not_to_be_confused_with=list(product.not_to_be_confused_with or []),
        competitors=[n for n in comp_names if n],
        scope_in=list(product.scope_in or []),
        scope_out=list(product.scope_out or []),
        goals=list(product.goals or []),
        regenerations={},
        drafting_error="",
        page_fetch_failed=False,
        updated_at="",
    )


@router.get("/products/{product_id}/profile", response_class=HTMLResponse)
def product_profile(request: Request, product_id: str, error: Optional[str] = None,
                    notice: Optional[str] = None):
    p = core._product_or_404(product_id)
    from pipeline import features as _features
    return core.templates.TemplateResponse(
        "wizard/step_profile.html",
        {
            "request": request,
            "product": p,
            "draft": _product_as_wizard_draft(p),
            "edit_mode": True,
            "regen_cap": 0,   # regen buttons hidden via edit_mode anyway
            "error": error,
            "notice": notice,
            "competition_analysis_enabled": _features.enabled(
                "competition_analysis_enabled",
            ),
        },
    )


@router.post("/products/{product_id}/profile")
async def product_profile_save(product_id: str, request: Request):
    product = core._product_or_404(product_id)
    form = await request.form()

    def _lines(name: str) -> list[str]:
        raw = form.get(name) or ""
        out, seen = [], set()
        for ln in raw.splitlines():
            s = ln.strip()
            if s and s not in seen:
                seen.add(s); out.append(s)
        return out

    # Competitors here are the chip textarea (one name per line) — matches
    # the wizard UX. Preserve rich attributes (aliases/color/context) for
    # names still present by matching case-insensitively against the
    # existing competitor list; new names get defaults; removed names drop.
    existing_by_lower = {
        (c.get("name") if isinstance(c, dict) else str(c) or "").strip().lower(): c
        for c in (product.competitors or [])
        if (c.get("name") if isinstance(c, dict) else str(c) or "").strip()
    }
    competitors: list[dict] = []
    for name in _lines("competitors"):
        key = name.lower()
        prev = existing_by_lower.get(key)
        if isinstance(prev, dict):
            # Preserve prior attrs; refresh the display name in case of casing edit.
            merged = dict(prev)
            merged["name"] = name
            competitors.append(merged)
        else:
            competitors.append({
                "name": name, "aliases": [], "color": None, "context": "",
            })

    facts = {
        "url": (form.get("url") or "").strip(),
        "aliases": _lines("aliases"),
        "not_to_be_confused_with": _lines("not_to_be_confused_with"),
        # Goals are not collected in the profile UI (removed from wizard
        # setup because no stage consumed them). Preserve whatever is on
        # disk so a Save doesn't wipe a legacy value.
        "goals": list(product.goals or []),
        "competitors": competitors,
        "scope_in": _lines("scope_in"),
        "scope_out": _lines("scope_out"),
    }
    try:
        save_product_facts(product_id, facts)
    except ValueError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/profile?error={str(e)[:120]}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/products/{product_id}/profile?notice=saved",
        status_code=303,
    )


# --- Delete product (configuration only) -----------------------------------
#
# Removes the products/<id>/ directory (taxonomy, prompts, sources,
# llm_routing, extras, examples, product.yaml). Reports (reports_root/<id>/),
# run logs (run_logs_root/<id>/), and warehouse data (data_root/<id>/) are
# NOT touched — they remain readable for anyone who still has a bookmarked
# report URL. Deletion is irreversible from the UI; users can rebuild the
# product via the wizard, but the new instance won't share the deleted config.

@router.post("/products/{product_id}/delete")
def product_delete(product_id: str, request: Request,
                   confirm_slug: str = Form(...)):
    """Delete the product's config directory. Requires the caller to type
    the slug back in `confirm_slug` as a safety interlock."""
    # Load first — 404 if the product doesn't exist. Bypass the LRU cache
    # to catch out-of-band deletes.
    core.clear_cache()
    p = core._product_or_404(product_id)
    if confirm_slug.strip() != product_id:
        return RedirectResponse(
            url=f"/products/{product_id}?error=confirmation+did+not+match",
            status_code=303,
        )
    target = p.dir
    # Belt-and-braces: refuse to delete anything outside PRODUCTS_DIR.
    try:
        target.resolve().relative_to(core.PRODUCTS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="refusing to delete a path outside PRODUCTS_DIR")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"{target} is not a directory")
    shutil.rmtree(target)
    core.clear_cache()
    return RedirectResponse(url="/?notice=deleted+" + product_id, status_code=303)


@router.get("/products/{product_id}/llm_routing", response_class=HTMLResponse)
def llm_routing_form(request: Request, product_id: str, saved: Optional[str] = None, error: Optional[str] = None):
    product = core._product_or_404(product_id)
    routing = product.llm_routing or {}
    # Pre-detect provider per stage from the saved endpoint so the form
    # can highlight the right option in the provider dropdown.
    detected = {
        stage: _detect_provider((routing.get(stage) or {}).get("endpoint", ""))
        for stage in ("relevance", "classify")
    }
    return core.templates.TemplateResponse(
        "llm_routing_form.html",
        {
            "request": request,
            "product": product,
            "routing": routing,
            "providers": core._llm_providers_for_template(),
            "detected_provider": detected,
            "saved": saved,
            "error": error,
        },
    )


@router.post("/products/{product_id}/llm_routing")
async def llm_routing_save(product_id: str, request: Request):
    product_dir = core._product_dir_for(product_id)
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
        meta = core.CONNECTION_META.get(provider) or {}
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
        core.clear_cache()
        load_product(product_id)
    except Exception as e:
        if routing_path.exists():
            routing_path.unlink()
        if backup_path.exists():
            backup_path.replace(routing_path)
        core.clear_cache()
        return RedirectResponse(
            url=f"/products/{product_id}/llm_routing?error={str(e)[:200]}",
            status_code=303,
        )
    if backup_path.exists():
        backup_path.unlink()
    return RedirectResponse(url=f"/products/{product_id}/llm_routing?saved=1", status_code=303)


def _detect_provider(endpoint: str) -> str:
    """Return the provider type id whose endpoint_hints match `endpoint`,
    or 'custom' if none match. Mirrors the client-side _matchProvider."""
    if not endpoint:
        return "custom"
    low = endpoint.lower()
    for type_id, meta in core.CONNECTION_META.items():
        if meta.get("category") != "llm":
            continue
        for hint in (meta.get("endpoint_hints") or []):
            if hint and hint.lower() in low:
                return type_id
    return "custom"
