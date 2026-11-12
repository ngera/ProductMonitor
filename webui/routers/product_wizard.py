"""Legacy product-creation wizard (/products/create/wizard/*).

Split out of webui/app.py (ADR-0026). Shared helpers live in
webui/core.py and are reached as `core.<name>` so a test patch there
reaches every caller.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pipeline.product import available_products, scaffold_product

from webui import core

router = APIRouter()


# --- Guided setup wizard (POST_V1_PLAN §4.3) --------------------------------
#
# Feature-flagged by `wizard_enabled`. Draft state lives at
# products/.wizard_drafts/<slug>.yaml so users can leave and resume.
# LLM-assist steps go through pipeline/wizard_llm.py with a 3-attempt
# regeneration cap per step.


@router.get("/products/create/wizard", response_class=HTMLResponse)
def wizard_landing(request: Request):
    """Wizard entry: list existing drafts + entry-point choices
    (fresh / clone)."""
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        return core.templates.TemplateResponse(
            "wizard.html",
            {"request": request, "flag_off": True, "step": None,
             "draft": None, "drafts": [], "products": [], "error": None},
        )
    drafts = _wiz.list_drafts(core.PRODUCTS_DIR)
    return core.templates.TemplateResponse(
        "wizard.html",
        {"request": request, "flag_off": False, "step": "landing",
         "draft": None, "drafts": drafts,
         "products": available_products(), "error": None},
    )


@router.post("/products/create/wizard/start")
async def wizard_start(request: Request):
    """Create a new draft from step-1 identity form."""
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        raise HTTPException(status_code=403, detail="wizard is disabled")

    form = await request.form()
    display = (form.get("display") or "").strip()
    if not display:
        return RedirectResponse(
            url="/products/create/wizard?error=display+is+required",
            status_code=303,
        )
    slug = _wiz.slugify(form.get("slug") or display)
    if (core.PRODUCTS_DIR / slug).exists():
        return RedirectResponse(
            url=f"/products/create/wizard?error=product+{slug}+already+exists",
            status_code=303,
        )
    existing = _wiz.load_draft(core.PRODUCTS_DIR, slug)
    if existing is not None:
        return RedirectResponse(
            url=f"/products/create/wizard/{slug}/identity",
            status_code=303,
        )
    draft = _wiz.WizardDraft(
        slug=slug, display=display,
        description=(form.get("description") or "").strip(),
        industry=(form.get("industry") or "").strip(),
        primary_goal=(form.get("primary_goal") or "").strip(),
        # first_run_solution.md §4.1 — pre-populate keyless-first sources
        # (HN search on the display name) so the user's first run works
        # without touching /connections.
        sources=_wiz.keyless_default_sources(slug, display),
    )
    _wiz.save_draft(core.PRODUCTS_DIR, draft)
    return RedirectResponse(
        url=f"/products/create/wizard/{slug}/scope",
        status_code=303,
    )


@router.post("/products/create/wizard/clone")
async def wizard_clone(request: Request):
    """Clone an existing product's taxonomy/prompts/snippets
    into a fresh draft."""
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        raise HTTPException(status_code=403, detail="wizard is disabled")
    form = await request.form()
    source = (form.get("source_product") or "").strip()
    display = (form.get("display") or "").strip()
    if not source or not display:
        return RedirectResponse(
            url="/products/create/wizard?error=source+and+display+required",
            status_code=303,
        )
    if not (core.PRODUCTS_DIR / source).exists():
        return RedirectResponse(
            url=f"/products/create/wizard?error=source+product+{source}+not+found",
            status_code=303,
        )
    slug = _wiz.slugify(form.get("slug") or display)
    if (core.PRODUCTS_DIR / slug).exists():
        return RedirectResponse(
            url=f"/products/create/wizard?error=slug+{slug}+already+exists",
            status_code=303,
        )
    draft = _wiz.clone_from(
        core.PRODUCTS_DIR / source, slug=slug, display=display,
        description=(form.get("description") or "").strip(),
    )
    # Seed keyless-first sources on clone too — cloned products keep
    # taxonomy/prompts but sources are inherently per-product (§4.1).
    if not draft.sources:
        draft.sources = _wiz.keyless_default_sources(slug, display)
    _wiz.save_draft(core.PRODUCTS_DIR, draft)
    return RedirectResponse(
        url=f"/products/create/wizard/{slug}/identity",
        status_code=303,
    )


@router.get("/products/create/wizard/{slug}/{step}", response_class=HTMLResponse)
def wizard_step(request: Request, slug: str, step: str):
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        raise HTTPException(status_code=403, detail="wizard is disabled")
    if not _wiz.is_valid_step(step):
        raise HTTPException(status_code=404, detail=f"unknown step {step!r}")
    draft = _wiz.load_draft(core.PRODUCTS_DIR, slug)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"no draft for {slug!r}")
    return core.templates.TemplateResponse(
        "wizard.html",
        {"request": request, "flag_off": False, "step": step,
         "draft": draft, "drafts": [], "products": [],
         "step_ids": _wiz.STEP_IDS, "step_labels": dict(_wiz.WIZARD_STEPS),
         "max_regenerations": _wiz.MAX_REGENERATIONS_PER_STEP, "error": None},
    )


@router.post("/products/create/wizard/{slug}/{step}")
async def wizard_step_save(slug: str, step: str, request: Request):
    """Persist step inputs into the draft and advance to the next step
    (or finalize on step 7)."""
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        raise HTTPException(status_code=403, detail="wizard is disabled")
    if not _wiz.is_valid_step(step):
        raise HTTPException(status_code=404, detail=f"unknown step {step!r}")
    draft = _wiz.load_draft(core.PRODUCTS_DIR, slug)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"no draft for {slug!r}")
    form = dict(await request.form())
    _apply_wizard_step(draft, step, form)
    _wiz.save_draft(core.PRODUCTS_DIR, draft)

    if step == "snippets":
        # Finalize: materialize + jump to product dashboard.
        try:
            product_dir = _wiz.materialize(
                draft, scaffold_fn=scaffold_product, products_dir=core.PRODUCTS_DIR,
            )
        except FileExistsError as e:
            return RedirectResponse(
                url=f"/products/create/wizard/{slug}/{step}?error={str(e)[:200]}",
                status_code=303,
            )
        core.clear_cache()
        return RedirectResponse(url=f"/products/{draft.slug}", status_code=303)

    nxt = _wiz.next_step(step)
    return RedirectResponse(
        url=f"/products/create/wizard/{slug}/{nxt}",
        status_code=303,
    )


@router.post("/products/create/wizard/{slug}/{step}/regenerate")
async def wizard_regenerate(slug: str, step: str, request: Request):
    """Call the assistant LLM to (re-)populate this step's fields.
    Enforces MAX_REGENERATIONS_PER_STEP (D from review)."""
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        raise HTTPException(status_code=403, detail="wizard is disabled")
    draft = _wiz.load_draft(core.PRODUCTS_DIR, slug)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"no draft for {slug!r}")
    if step not in _wiz.LLM_ASSISTED_STEPS:
        raise HTTPException(status_code=400, detail=f"step {step!r} has no LLM assist")
    if not draft.can_regenerate(step):
        return RedirectResponse(
            url=f"/products/create/wizard/{slug}/{step}?error=regeneration+cap+reached+({_wiz.MAX_REGENERATIONS_PER_STEP})",
            status_code=303,
        )

    from pipeline import wizard_llm as _wlm
    error = None
    if step == "scope":
        result = _wlm.suggest_scope(draft.description or draft.display)
        if result: draft.scope_in, draft.scope_out = result.scope_in, result.scope_out
        else: error = "assistant+LLM+unavailable"
    elif step == "taxonomy":
        result = _wlm.suggest_taxonomy(draft.description, draft.scope_in, draft.scope_out)
        if result:
            draft.areas = [a.model_dump() for a in result.areas]
        else: error = "assistant+LLM+unavailable"
    elif step == "prompts":
        result = _wlm.suggest_prompts(draft.description, draft.scope_in, draft.areas)
        if result:
            draft.prompts = {
                "relevance": {
                    "system": result.relevance_system,
                    "template": result.relevance_template,
                    "few_shot": {"enabled": True, "n_positive": 3, "n_negative": 2},
                },
                "classify": {
                    "system": result.classify_system,
                    "template": result.classify_template,
                    "extras_instructions": "",
                    "few_shot": {"enabled": True, "n_positive": 2, "n_negative": 1},
                },
            }
        else: error = "assistant+LLM+unavailable"
    elif step == "snippets":
        result = _wlm.suggest_snippets(draft.description, draft.areas)
        if result:
            draft.snippets = [{
                "polarity": s.polarity,
                "title": s.title,
                "body": s.body,
                "labels": s.labels,
                "notes": "seeded by wizard",
            } for s in result.snippets]
        else: error = "assistant+LLM+unavailable"

    draft.note_regeneration(step)
    _wiz.save_draft(core.PRODUCTS_DIR, draft)
    url = f"/products/create/wizard/{slug}/{step}"
    if error:
        url += f"?error={error}"
    return RedirectResponse(url=url, status_code=303)


def _apply_wizard_step(draft, step: str, form: dict) -> None:
    """Copy form fields into the draft for the given step. Trivial
    per-field mapping; validation is loose to allow partial saves."""
    if step == "identity":
        draft.display = (form.get("display") or draft.display).strip()
        draft.description = (form.get("description") or draft.description).strip()
        draft.industry = (form.get("industry") or "").strip()
        draft.primary_goal = (form.get("primary_goal") or "").strip()
    elif step == "scope":
        draft.scope_in = (form.get("scope_in") or "").strip()
        draft.scope_out = (form.get("scope_out") or "").strip()
    elif step == "taxonomy":
        # Form uses `areas_json`; if present, we replace areas wholesale.
        raw = form.get("areas_json")
        if raw:
            import json as _j
            try:
                parsed = _j.loads(raw)
                if isinstance(parsed, list):
                    draft.areas = parsed
            except Exception:
                pass
    elif step == "prompts":
        # Prompts saved via 4 free-text fields.
        draft.prompts = {
            "relevance": {
                "system": form.get("relevance_system") or "",
                "template": form.get("relevance_template") or "",
                "few_shot": {"enabled": True, "n_positive": 3, "n_negative": 2},
            },
            "classify": {
                "system": form.get("classify_system") or "",
                "template": form.get("classify_template") or "",
                "extras_instructions": "",
                "few_shot": {"enabled": True, "n_positive": 2, "n_negative": 1},
            },
        }
    elif step == "sources":
        # Free-text summary; user can hydrate real sources on the product's
        # sources page. Wizard doesn't try to be the full sources editor.
        raw = form.get("sources_json")
        if raw:
            import json as _j
            try:
                parsed = _j.loads(raw)
                if isinstance(parsed, list):
                    draft.sources = parsed
            except Exception:
                pass
    elif step == "llm":
        # first_run_solution.md §4.2 — the 3-button LLM chooser. Fields:
        #   llm_choice ∈ {hosted, ollama, skip}
        #   for hosted: llm_provider (anthropic | openai | gemini | azure_openai)
        #                 llm_api_key (written into .env under the provider's env var)
        #   for ollama: llm_endpoint / llm_model (both optional; defaults are fine)
        from pipeline import wizard as _wiz_mod
        choice = (form.get("llm_choice") or "").strip()
        if choice not in _wiz_mod.LLM_CHOICES:
            return  # ignore invalid submissions; keep prior state
        draft.llm_choice = choice
        draft.llm_endpoint = (form.get("llm_endpoint") or "").strip()
        draft.llm_model = (form.get("llm_model") or "").strip()
        draft.llm_provider = (form.get("llm_provider") or "").strip()

        if choice == _wiz_mod.LLM_CHOICE_HOSTED:
            preset = _wiz_mod.PROVIDER_PRESETS.get(draft.llm_provider or "", {})
            if preset:
                if not draft.llm_endpoint:
                    draft.llm_endpoint = preset["endpoint"]
                if not draft.llm_model:
                    draft.llm_model = preset["model"]
                api_key = (form.get("llm_api_key") or "").strip()
                if api_key:
                    # Persist into .env under the provider's env var name so
                    # the pipeline's _resolve_api_key picks it up on the next run.
                    _write_env_var(preset["api_key_env"], api_key)
        elif choice == _wiz_mod.LLM_CHOICE_OLLAMA:
            draft.llm_endpoint = draft.llm_endpoint or _wiz_mod.DEFAULT_OLLAMA_ENDPOINT
            draft.llm_model = draft.llm_model or _wiz_mod.DEFAULT_OLLAMA_MODEL

        # Live health check (skipped for "skip"). Kept optional — a failed
        # check surfaces a message but doesn't block advancing, because
        # users may want to save the key first and troubleshoot separately.
        if choice == _wiz_mod.LLM_CHOICE_SKIP:
            draft.llm_health_ok = None
            draft.llm_health_message = ""
        else:
            ok, msg = _wizard_llm_health_check(draft)
            draft.llm_health_ok = ok
            draft.llm_health_message = msg
    elif step == "snippets":
        # No user-edit path at this step in the minimal cut; regenerate + accept.
        pass


def _write_env_var(env_name: str, value: str) -> None:
    """Persist an API key to .env AND sync os.environ so the running
    process picks it up immediately (no restart required). Delegates to
    pipeline.env_writer.set_var for the write+sync pairing."""
    if not env_name or not value:
        return
    from pipeline import env_writer
    env_writer.set_var(core.ENV_FILE_PATH, env_name, value)


def _wizard_llm_health_check(draft) -> tuple[bool, str]:
    """Run LLMClient.health_check against the draft's chosen LLM.

    Returns (ok, message). Never raises — a health check is diagnostic, not
    a gate on advancing through the wizard.
    """
    from pipeline import wizard as _wiz_mod
    routing = _wiz_mod.build_llm_routing(draft)
    if not routing:
        return False, "endpoint or model missing"

    # Minimal shim: build an LLMClient with an inline llm_routing that
    # doesn't touch product state. We do this by temporarily setting the
    # app_config's `llm` block, then constructing the client with role
    # 'relevance' (health checks are role-agnostic since they hit
    # chat.completions.create).
    from pipeline.llm import LLMClient
    from pipeline.config import app_config as _app_config

    app = _app_config()
    saved_llm = app.get("llm")
    app["llm"] = routing
    try:
        client = LLMClient("relevance")
        ok = bool(client.health_check())
        return (True, "endpoint reachable") if ok else (
            False, "health check failed — check endpoint URL and API key",
        )
    except Exception as e:
        return False, f"could not initialize client: {e}"
    finally:
        if saved_llm is None:
            app.pop("llm", None)
        else:
            app["llm"] = saved_llm
