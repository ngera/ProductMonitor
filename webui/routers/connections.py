"""Source and LLM connections, plus local Ollama management.

Split out of webui/app.py (ADR-0026). Shared helpers live in
webui/core.py and are reached as `core.<name>` so a test patch there
reaches every caller.
"""

from __future__ import annotations

import json as _json
import yaml
from dotenv import dotenv_values
from fastapi import APIRouter, Body, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from typing import Optional

from webui import core

router = APIRouter()


# ScrapeCreators plugins all share ONE API key (SCRAPECREATORS_API_KEY) via
# the shared client in sources/scrapecreators/client.py. On the Connections
# page we collapse the three sub-plugin rows into this single synthetic
# entry so users don't see three redundant "1 field" rows for the same key.
# The individual scrapecreators_reddit/x/tiktok manifests still exist for
# product sources.yaml, the "add source" picker, and per-plugin stream config.
_SCRAPECREATORS_SUBPLUGINS = ("scrapecreators_reddit", "scrapecreators_x", "scrapecreators_tiktok")


def _read_env() -> dict[str, str]:
    """Return current values in .env (empty dict if the file doesn't exist)."""
    if not core.ENV_FILE_PATH.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(str(core.ENV_FILE_PATH)).items()}


def _connection_status(type_id: str, env: dict[str, str]) -> str:
    """One-word status for the list view: configured / partial / not-needed / missing."""
    meta = core.CONNECTION_META.get(type_id, {})
    fields = meta.get("fields") or []
    if not fields:
        return "not-needed"
    set_fields = sum(1 for f in fields if (env.get(f["env"]) or "").strip())
    if set_fields == len(fields):
        return "configured"
    if set_fields == 0:
        return "missing"
    return "partial"


# Display metadata for the ADR-0021 source_category taxonomy. `id` is
# what the template loops over; `display` is the section header. `plugin_ids`
# is derived at request time from each plugin's manifest — no hand-maintained
# set. The synthetic "scrapecreators" row is always classified as
# third_party_scraper via the manual mapping in _source_group_id.
SOURCE_GROUPS: list[dict] = [
    {
        "id": "rss_feed",
        "display": "RSS feeds",
        "description": "Keyless — no API key needed. Fetched via RSS/Atom.",
    },
    {
        "id": "third_party_scraper",
        "display": "Third-party scrapers",
        "description": "Social platforms (Reddit, X, TikTok) fetched via a shared scraper API vendor.",
    },
    {
        "id": "custom_source",
        "display": "Custom sources",
        "description": "Each plugin requires its own API key or OAuth credentials. Configure per plugin.",
    },
]


# Synthetic rollup rows (scrapecreators) don't have a real manifest to read
# source_category from, so we map them explicitly.
_SYNTHETIC_TYPE_TO_CATEGORY = {
    "scrapecreators": "third_party_scraper",
}


def _source_group_id(type_id: str) -> str:
    """Map a plugin id to its display group by reading its manifest's
    `source_category`. Falls back to `custom_source` for unknown ids so the
    row shows up on the page rather than silently disappearing.
    """
    if type_id in _SYNTHETIC_TYPE_TO_CATEGORY:
        return _SYNTHETIC_TYPE_TO_CATEGORY[type_id]
    try:
        from sources.registry import get_registry
        p = get_registry().get(type_id)
        if p is not None:
            return getattr(p.manifest, "source_category", "custom_source")
    except Exception:
        pass
    return "custom_source"


def _row_for_connection(type_id: str, meta: dict, env: dict, paused_types: set) -> dict:
    """Uniform row shape for both the sources and LLM connection tables."""
    return {
        "type": type_id,
        "display": meta.get("display") or type_id,
        "url": meta.get("url") or "",
        "n_fields": len(meta.get("fields") or []),
        "status": _connection_status(type_id, env),
        "paused": type_id in paused_types,
        # Only source-type rows get a pause toggle; LLM providers don't.
        "can_pause": meta.get("category") == "source",
    }


@router.get("/connections", response_class=HTMLResponse)
def connections_index(request: Request):
    """Sources tab (default landing at /connections). LLM providers +
    assistant LLM live on /connections/llms."""
    env = _read_env()
    from sources import available_source_types
    from pipeline import connections as _conn
    from pipeline import media_sources as _media

    available_sources = set(available_source_types())
    globally_paused = _conn.paused_types()

    source_rows: list[dict] = []
    for type_id, meta in core.CONNECTION_META.items():
        if meta.get("category") == "source" and type_id in available_sources:
            source_rows.append(_row_for_connection(type_id, meta, env, globally_paused))

    # Collapse the 3 ScrapeCreators sub-plugin rows into ONE synthetic row.
    # They share SCRAPECREATORS_API_KEY (via sources/scrapecreators/client.py),
    # so exposing three identical "1 field" entries just confuses users.
    sc_present = any(p in available_sources for p in _SCRAPECREATORS_SUBPLUGINS)
    source_rows = [r for r in source_rows if r["type"] not in _SCRAPECREATORS_SUBPLUGINS]
    if sc_present:
        sc_meta = core.CONNECTION_META["scrapecreators"]
        sc_row = _row_for_connection("scrapecreators", sc_meta, env, globally_paused)
        # Synthetic row is "paused" only when every sub-plugin is paused —
        # partial pause (e.g. only TikTok muted) shows as active with a note.
        sc_row["paused"] = all(p in globally_paused for p in _SCRAPECREATORS_SUBPLUGINS)
        source_rows.append(sc_row)

    source_rows.sort(key=lambda r: r["display"])

    grouped_sources: dict[str, list[dict]] = {g["id"]: [] for g in SOURCE_GROUPS}
    for r in source_rows:
        gid = _source_group_id(r["type"])
        # Unknown category → drop into custom_source rather than KeyError.
        if gid not in grouped_sources:
            gid = "custom_source"
        grouped_sources[gid].append(r)

    # Also expose plugin content_types so the template can render chips.
    from sources.registry import get_registry as _reg
    _r = _reg()
    for rows in grouped_sources.values():
        for row in rows:
            plugin = _r.get(row["type"])
            if plugin is not None:
                row["content_types"] = list(getattr(plugin.manifest, "content_types", []))
            else:
                row["content_types"] = []

    media_source_rows = _media.load()

    return core.templates.TemplateResponse(
        "connections_index.html",
        {
            "request": request,
            "source_rows": source_rows,
            "source_groups": SOURCE_GROUPS,
            "grouped_sources": grouped_sources,
            "media_sources": media_source_rows,
            "env_file": ".env",
            "admin_active": "sources",
        },
    )


@router.get("/connections/llms", response_class=HTMLResponse)
def connections_llms(request: Request):
    """LLM Connections tab — sibling to /connections (which is the Sources
    tab). Split out from the original /connections page in a UX pass because
    the combined view had become long."""
    env = _read_env()
    from pipeline import admin_defaults as _defaults
    from pipeline import assistant_llm as _al
    from pipeline import features as _features

    llm_rows: list[dict] = []
    for type_id, meta in core.CONNECTION_META.items():
        if meta.get("category") == "llm":
            # LLM rows don't participate in the source pause list.
            llm_rows.append(_row_for_connection(type_id, meta, env, set()))
    llm_rows.sort(key=lambda r: r["display"])

    cfg = _al.current_config()
    assistant_summary = {
        "configured": cfg is not None,
        "endpoint": cfg.endpoint if cfg else "",
        "model": cfg.model if cfg else "",
        "budget_usd": cfg.budget_usd_per_product_per_month if cfg else 0.0,
        "api_key_env": cfg.api_key_env if cfg else "",
        "enabled": _features.enabled("assistant_llm_enabled"),
    }

    # Only *configured* rows are selectable so we don't let the admin
    # nominate a provider whose key isn't set.
    llm_default_options = [
        {"type": r["type"], "display": r["display"]}
        for r in llm_rows if r["status"] == "configured"
    ]
    current_default = _defaults.default_llm_provider()

    return core.templates.TemplateResponse(
        "connections_llms.html",
        {
            "request": request,
            "llm_rows": llm_rows,
            "env_file": ".env",
            "assistant_summary": assistant_summary,
            "admin_active": "llms",
            "llm_default_options": llm_default_options,
            "current_default": current_default,
            "default_saved": request.query_params.get("default_saved") == "1",
        },
    )


@router.post("/connections/default_llm")
def connections_set_default_llm(provider: str = Form("")):
    """Persist the admin's choice of default LLM provider — the one the
    wizard's Review screen pre-selects. Empty string clears the default."""
    from pipeline import admin_defaults as _defaults
    _defaults.set_default_llm_provider(provider.strip() or None)
    # Redirects to the LLM Connections tab (where the default LLM selector
    # lives after the sources/llms split).
    return RedirectResponse(url="/connections/llms?default_saved=1", status_code=303)


# POST_V1_PLAN §4.8 — dedicated assistant LLM form (must register BEFORE
# the generic /connections/{type_id} route so FastAPI matches this first).


@router.get("/connections/assistant_llm", response_class=HTMLResponse)
def assistant_llm_form(request: Request, saved: int = 0, error: Optional[str] = None):
    """Dedicated form for the global assistant LLM (POST_V1_PLAN §4.8)."""
    from pipeline import assistant_llm as _al

    cfg = _al.current_config()
    return core.templates.TemplateResponse(
        "assistant_llm_form.html",
        {
            "request": request,
            "cfg": cfg,
            "configured": _al.is_configured(),
            "llm_providers": core._llm_providers_for_template(),
            "saved": bool(saved),
            "error": error,
        },
    )


@router.post("/connections/assistant_llm")
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


@router.get("/connections/{type_id}", response_class=HTMLResponse)
def connection_form(request: Request, type_id: str, error: Optional[str] = None,
                     saved: Optional[str] = None, assistant: Optional[str] = None):
    if type_id not in core.CONNECTION_META:
        raise HTTPException(status_code=404, detail=f"unknown source type: {type_id}")
    meta = core.CONNECTION_META[type_id]
    env = _read_env()
    values: dict[str, str] = {}
    for f in meta.get("fields") or []:
        values[f["env"]] = env.get(f["env"], "") or f.get("default", "")

    # Is this connection currently the one powering the assistant LLM?
    # Compare endpoints (case-insensitive, ignore trailing slash) so an
    # admin who set up the assistant via this same provider sees the
    # checkbox already checked.
    is_current_assistant = False
    if meta.get("category") == "llm":
        try:
            from pipeline import assistant_llm as _al
            cfg = _al.current_config()
            if cfg and cfg.endpoint:
                a = cfg.endpoint.rstrip("/").lower()
                b = (meta.get("api_endpoint") or "").rstrip("/").lower()
                is_current_assistant = bool(a and b and a == b)
        except Exception:
            pass

    return core.templates.TemplateResponse(
        "connection_form.html",
        {
            "request": request,
            "type_id": type_id,
            "meta": meta,
            "values": values,
            "error": error,
            "saved": saved,
            "assistant_saved": assistant == "1",
            "is_current_assistant": is_current_assistant,
            "env_file": ".env",
        },
    )


# --- Ollama lifecycle API ---------------------------------------------------


@router.post("/api/ollama/ensure-running")
def api_ollama_ensure_running(payload: dict = Body(default={})):
    """Detect Ollama, spawn `ollama serve` if needed, return readiness +
    pulled-models list. Called from the connections/ollama page and from
    the LLM routing form on save."""
    from pipeline import ollama_lifecycle
    base_url = (payload or {}).get("base_url") or "http://localhost:11434"
    required_model = (payload or {}).get("required_model") or None
    # resolve_base_url (inside ensure_running) rewrites localhost →
    # host.docker.internal when the UI runs in Docker.
    return ollama_lifecycle.ensure_running(base_url=base_url, required_model=required_model)


@router.post("/api/ollama/pull")
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


@router.post("/api/ollama/install")
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


@router.post("/connections/{type_id}")
async def connection_save(type_id: str, request: Request):
    if type_id not in core.CONNECTION_META:
        raise HTTPException(status_code=404, detail=f"unknown source type: {type_id}")
    meta = core.CONNECTION_META[type_id]
    form = await request.form()

    # Make sure the .env file exists; dotenv.set_key creates it if absent
    # but its parent must exist. Project root always does.
    core.ENV_FILE_PATH.touch(exist_ok=True)

    # Capture the API key BEFORE writing it — we may also copy it to
    # ASSISTANT_LLM_API_KEY below if the user asked to reuse this
    # connection for the assistant LLM.
    saved_secrets: dict[str, str] = {}

    try:
        from pipeline import env_writer
        for f in meta.get("fields") or []:
            env_name = f["env"]
            new_val = (form.get(env_name) or "").strip()
            # Empty value -> unset the key entirely (cleaner than KEY=).
            # env_writer keeps os.environ + the .env file in sync so the
            # running process reads the new value on the NEXT request
            # rather than after a restart.
            env_writer.set_or_unset(core.ENV_FILE_PATH, env_name, new_val)
            if new_val:
                saved_secrets[env_name] = new_val
    except Exception as e:
        return RedirectResponse(
            url=f"/connections/{type_id}?error={str(e)[:200]}",
            status_code=303,
        )

    # "Use for Assistant LLM": one-click way to point the assistant LLM at
    # the same provider using the same key. Copies endpoint + default model
    # into config/assistant_llm.yaml AND writes ASSISTANT_LLM_API_KEY to
    # .env so the assistant reads from its own env slot (kept distinct from
    # the connections env var — same key, different var, so the two can be
    # rotated independently later).
    use_for_assistant = (form.get("use_for_assistant") == "on"
                          and meta.get("category") == "llm")
    if use_for_assistant:
        try:
            from pipeline import assistant_llm as _al
            from pipeline import features as _features
            endpoint = meta.get("api_endpoint", "")
            # Prefer the classify-tier recommended model if one is listed;
            # otherwise fall back to the first recommendation.
            models = meta.get("recommended_models") or []
            default_model = ""
            for m in models:
                if "recommended default" in (m.get("purpose") or "").lower():
                    default_model = m.get("id", ""); break
            if not default_model and models:
                default_model = models[0].get("id", "")
            if endpoint and default_model:
                cfg = _al.AssistantLLMConfig(
                    endpoint=endpoint, model=default_model,
                    api_key_env="ASSISTANT_LLM_API_KEY",
                )
                _al.save_config(cfg)
                # Copy the just-saved API key value into ASSISTANT_LLM_API_KEY
                # so the assistant sees it immediately.
                for env_name, value in saved_secrets.items():
                    if value:
                        env_writer.set_var(core.ENV_FILE_PATH,
                                           "ASSISTANT_LLM_API_KEY", value)
                        break
                # Turn on the flag so the wizard actually uses it.
                try:
                    _flip_flag_on("assistant_llm_enabled")
                except Exception:
                    pass
        except Exception as e:
            return RedirectResponse(
                url=f"/connections/{type_id}?error=assistant+copy+failed:+{str(e)[:120]}",
                status_code=303,
            )

    return RedirectResponse(
        url=(f"/connections/{type_id}?saved=1"
              + ("&assistant=1" if use_for_assistant else "")),
        status_code=303,
    )


def _flip_flag_on(name: str) -> None:
    """Turn a feature flag ON in config/features.yaml. Used by
    connection_save when the admin asks to reuse the connection for the
    assistant LLM — we don't want to turn it on without clearing the
    'not configured' banner in the wizard."""
    from pipeline import features as _features
    try:
        data = yaml.safe_load(core._FEATURES_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    flags = data.get("features") or {}
    if flags.get(name) is True:
        return
    flags[name] = True
    data["features"] = flags
    tmp = core._FEATURES_YAML.with_suffix(core._FEATURES_YAML.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
                   encoding="utf-8")
    tmp.replace(core._FEATURES_YAML)
    _features.clear_cache()


@router.post("/connections/{type_id}/pause")
def connection_toggle_pause(type_id: str, paused: str = Form(...)):
    """Set the global pause state for a source type.

    Called from the /connections index page's per-row pause form. `paused`
    is 'true' or 'false' (string form value). Only source-type connections
    can be paused — LLM providers are always active. Precedence: the global
    pause here overrides any product-level `paused: false`.

    Special case: `type_id == "scrapecreators"` is a synthetic UI row
    covering the 3 real ScrapeCreators sub-plugins. Pause/unpause fans out
    to all three so the visible "paused" state on the collapsed row matches
    reality.
    """
    if type_id not in core.CONNECTION_META:
        raise HTTPException(status_code=404, detail=f"unknown connection type: {type_id}")
    if core.CONNECTION_META[type_id].get("category") != "source":
        raise HTTPException(status_code=400, detail="only source connections can be paused")
    from pipeline import connections as _conn
    is_paused = paused.lower() in ("true", "1", "on", "yes")
    if type_id == "scrapecreators":
        for sub in _SCRAPECREATORS_SUBPLUGINS:
            _conn.set_paused(sub, is_paused)
    else:
        _conn.set_paused(type_id, is_paused)
    return RedirectResponse(url="/connections", status_code=303)
