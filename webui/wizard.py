"""Wizard v2 router (wizard redesign Phase 3).

Feature-flagged by `wizard_v2_enabled`. Mounted from webui/app.py via
`app.include_router(router)`. Kept separate from the existing v1 routes in
app.py so we can flip the flag and delete v1 without a big rewrite.

Routes exposed:
    GET  /wizard                          → screen 1 (or resume list)
    POST /wizard/draft                    → create draft + run ProfileDraft
    GET  /wizard/{slug}                   → render the draft's current step
    POST /wizard/{slug}/profile           → save Screen-2 edits
    POST /wizard/{slug}/regen/{section}   → regenerate one section (cap-checked)
    POST /wizard/{slug}/discard           → delete a draft

Screens 3 (calibrate) and 4 (review) are added in Phases 4 and 5. This
router serves a friendly placeholder for those steps for now.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from pipeline import assistant_llm as _assistant_llm
from pipeline import features as _features
from pipeline import minifetch as _minifetch
from pipeline import profile_draft as _profile_draft
from pipeline import taxonomy_proposal as _tax_prop
from pipeline import wizard_v2 as _wv2
from pipeline.product import (
    PRODUCTS_DIR,
    VALID_GOALS,
    available_products,
)


router = APIRouter()

_TEMPLATES = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent / "templates"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_flag():
    """Raise 403 when wizard v2 is off. Every route calls this first."""
    if not _features.enabled("wizard_v2_enabled"):
        raise HTTPException(status_code=403, detail="wizard v2 is disabled")


def _products_dir() -> Path:
    # Re-read the module attr each time so tests that monkeypatch it work.
    from pipeline import product as _p
    return _p.PRODUCTS_DIR


def _render(request: Request, template: str, **ctx) -> HTMLResponse:
    """TemplateResponse with the request bound (starlette requires it).

    Also seeds a small set of global feature flags every wizard template
    can consult without each route having to pass them explicitly.
    """
    from pipeline import features as _features
    ctx.setdefault(
        "competition_analysis_enabled",
        _features.enabled("competition_analysis_enabled"),
    )
    return _TEMPLATES.TemplateResponse(request, template, ctx)


def _get_draft_or_404(slug: str) -> _wv2.WizardV2Draft:
    draft = _wv2.load_draft(_products_dir(), slug)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"draft {slug!r} not found")
    return draft


# ---------------------------------------------------------------------------
# Assistant-LLM setup wizard (standalone)
# ---------------------------------------------------------------------------
#
# A dedicated one-page wizard for setting up the global assistant LLM
# (endpoint + model + API key). Not gated by `wizard_v2_enabled` — always
# reachable so users can configure the assistant LLM before or after
# starting the product wizard.
#
# Every provider preset writes to two places on save:
#   1. `.env`  — the provider's API key env var
#   2. `config/assistant_llm.yaml` — endpoint + model + budget
# Plus it flips `assistant_llm_enabled=true` in `config/features.yaml`
# so the wizard drafting service picks up the connection immediately.


# Provider-agnostic env var for the assistant LLM. Only ONE assistant LLM
# is active at a time (config/assistant_llm.yaml), so switching providers
# just overwrites this variable — no per-provider naming clutter in .env.
# The `api_key_env` field on each provider below is the *connections* key,
# distinct so /connections/<provider> and the assistant can hold different
# keys per install.
ASSISTANT_LLM_API_KEY_ENV = "ASSISTANT_LLM_API_KEY"


_LLM_WIZARD_PROVIDERS = [
    {
        "id": "anthropic",
        "display": "Anthropic (Claude)",
        # Trailing slash is required by Anthropic's OpenAI-compat layer —
        # without it the SDK builds URLs like `.../v1chat/completions`.
        "endpoint": "https://api.anthropic.com/v1/",
        "default_model": "claude-haiku-4-5-20251001",
        "recommended_models": [
            ("claude-haiku-4-5-20251001", "Haiku — fast & cheap (~$1/mo default budget)"),
            ("claude-sonnet-4-6", "Sonnet — higher quality for taxonomy/prompts"),
        ],
        "api_key_env": "ANTHROPIC_API_KEY",
        "needs_key": True,
        "key_help": "Get one at https://console.anthropic.com/account/keys",
    },
    {
        "id": "openai",
        "display": "OpenAI",
        "endpoint": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "recommended_models": [
            ("gpt-4o-mini", "gpt-4o-mini — fast & cheap"),
            ("gpt-4o", "gpt-4o — higher quality"),
        ],
        "api_key_env": "OPENAI_API_KEY",
        "needs_key": True,
        "key_help": "Get one at https://platform.openai.com/api-keys",
    },
    {
        "id": "gemini",
        "display": "Google Gemini",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "default_model": "gemini-2.0-flash",
        "recommended_models": [
            ("gemini-2.0-flash", "Gemini 2.0 Flash — fast & cheap"),
            ("gemini-1.5-pro", "Gemini 1.5 Pro — higher quality"),
        ],
        "api_key_env": "GOOGLE_API_KEY",
        "needs_key": True,
        "key_help": "Get one at https://aistudio.google.com/app/apikey",
    },
    {
        "id": "ollama",
        "display": "Local (Ollama)",
        "endpoint": "http://localhost:11434/v1",
        "default_model": "llama3.1:8b",
        "recommended_models": [
            ("llama3.1:8b", "llama3.1:8b — good general model"),
            ("qwen2.5:7b", "qwen2.5:7b — strong instruction follower"),
        ],
        "api_key_env": "",
        "needs_key": False,
        "key_help": "No API key needed. Ollama must be running locally (ollama serve).",
    },
]


def _assistant_key_env_for(provider_id: str) -> str:
    """Return the env var the assistant LLM writes/reads to for a given
    provider. Currently a single `ASSISTANT_LLM_API_KEY` regardless of
    provider (only one assistant is active at a time). Ollama-style
    providers return "" (no key needed)."""
    provider = _providers_by_id().get(provider_id, {})
    if not provider.get("needs_key", True):
        return ""
    return ASSISTANT_LLM_API_KEY_ENV


def _providers_by_id() -> dict:
    return {p["id"]: p for p in _LLM_WIZARD_PROVIDERS}


def _apply_inline_stream_config(draft, form, view_by_id: dict) -> None:
    """Parse the Step 3 form into each source's `stream_config`.

    Two form field families per required field:
      - `suggest__<plugin_id>__<field_name>` — checkbox values from the
        LLM-suggested list. Multiple checked → multiple identifiers.
      - `stream__<plugin_id>__<field_name>` — textarea for user-added
        values (one per line).

    Merged list per field: checked suggestions first, then textarea
    lines (deduped). Empty result → the field is cleared and materialize
    will skip that source.

    Contract per required field:
      - text / number: each entry = one stream.
      - csv: each entry = one stream's csv value.

    First entry sits directly on `stream_config[field]`; extras go into
    `stream_config["_extra_streams"]` which `_source_entry` and
    `_run_minifetch` expand into multiple stream entries.
    """
    for src in draft.suggested_sources:
        pid = src.get("plugin_id")
        if not pid:
            continue
        # Media Coverage Sources (rss) is configured via a catalog subset
        # picker, not the plain textarea. Read `catalog_url` picks and
        # translate them into stream_config (first URL as base, rest as
        # _extra_streams). See _build_sources_view + step_sources.html.
        if pid == "rss":
            picked_urls = [u.strip() for u in form.getlist("catalog_url")
                           if u and u.strip()]
            cfg = dict(src.get("stream_config") or {})
            # Drop prior catalog entries; a re-save with no picks clears rss.
            cfg.pop("_extra_streams", None)
            if not picked_urls:
                cfg.pop("feed_url", None)
                src["stream_config"] = cfg
                continue
            cfg["feed_url"] = picked_urls[0]
            cfg.setdefault("name", f"rss-{draft.slug}")
            extras: list[dict] = []
            for i, url in enumerate(picked_urls[1:], start=2):
                extras.append({
                    "name": f"rss-{draft.slug}-{i}",
                    "feed_url": url,
                })
            if extras:
                cfg["_extra_streams"] = extras
            src["stream_config"] = cfg
            continue
        v = view_by_id.get(pid)
        if not v or not v.get("required_stream_fields"):
            continue
        cfg = dict(src.get("stream_config") or {})
        extra_streams: list[dict] = []
        field_lines: dict[str, list[str]] = {}
        for f in v["required_stream_fields"]:
            # Checked suggestions.
            checked = form.getlist(f"suggest__{pid}__{f['name']}")
            checked = [c.strip() for c in checked if c and c.strip()]
            # Textarea additions.
            key = f"stream__{pid}__{f['name']}"
            raw = form.get(key)
            typed_lines: list[str] = []
            if raw is not None:
                typed_lines = [ln.strip() for ln in str(raw).splitlines()
                               if ln.strip()]
            # Per-plugin normalization: reddit_rss stores subreddits as bare
            # names on disk, but the user may type r/foo, /r/foo, or paste
            # a full Reddit URL. Strip everything back to `foo`.
            if pid == "reddit_rss" and f["name"] == "subreddit":
                from sources.reddit_rss import normalize_subreddit
                checked = [n for n in (normalize_subreddit(c) for c in checked) if n]
                typed_lines = [n for n in (normalize_subreddit(l) for l in typed_lines) if n]
            if pid == "discourse" and f["name"] == "host":
                from sources.discourse import normalize_host
                checked = [n for n in (normalize_host(c) for c in checked) if n]
                typed_lines = [n for n in (normalize_host(l) for l in typed_lines) if n]
            if pid == "stackex" and f["name"] == "site":
                from sources.stackex import normalize_site
                checked = [n for n in (normalize_site(c) for c in checked) if n]
                typed_lines = [n for n in (normalize_site(l) for l in typed_lines) if n]
            if pid == "microsoft_community" and f["name"] == "feed_url":
                from sources.microsoft_community import _normalize_feed_url
                checked = [n for n in (_normalize_feed_url(c) for c in checked) if n]
                typed_lines = [n for n in (_normalize_feed_url(l) for l in typed_lines) if n]
            # Merge: checked first, then unique typed lines.
            merged: list[str] = []
            seen: set[str] = set()
            for v_ in checked + typed_lines:
                if v_ in seen:
                    continue
                seen.add(v_); merged.append(v_)
            field_lines[f["name"]] = merged
            if not merged:
                cfg.pop(f["name"], None)
                continue
            first = merged[0]
            if f["type"] == "csv":
                cfg[f["name"]] = [v.strip() for v in first.split(",") if v.strip()]
            else:
                cfg[f["name"]] = first

        # Build additional streams from entries 2..N of every required field.
        # For a source with multiple required fields, lines line up by index —
        # line 2 of `subreddit` pairs with (implicit blank) line 2 of the
        # other fields. In practice most sources have only one required field
        # so this behavior is straightforward.
        max_extra = max((len(lines) - 1 for lines in field_lines.values()), default=0)
        for i in range(max_extra):
            extra_cfg = {}
            for f in v["required_stream_fields"]:
                lines = field_lines.get(f["name"]) or []
                if i + 1 >= len(lines):
                    continue
                val = lines[i + 1]
                if f["type"] == "csv":
                    extra_cfg[f["name"]] = [v.strip() for v in val.split(",") if v.strip()]
                else:
                    extra_cfg[f["name"]] = val
            if extra_cfg:
                # Auto-generate a name so the pipeline's cursor logic
                # keeps them distinct.
                extra_cfg["name"] = f"{pid}-{draft.slug}-{i + 2}"
                extra_streams.append(extra_cfg)
        if extra_streams:
            cfg["_extra_streams"] = extra_streams
        else:
            cfg.pop("_extra_streams", None)
        # Discourse search mode needs a query; seed from the product name
        # when the operator only filled Forum host.
        if pid == "discourse" and cfg.get("host"):
            cfg.setdefault("mode", "search")
            cfg.setdefault("query", draft.display or draft.slug)
            for extra in cfg.get("_extra_streams") or []:
                if isinstance(extra, dict) and extra.get("host"):
                    extra.setdefault("mode", "search")
                    extra.setdefault("query", draft.display or draft.slug)
        src["stream_config"] = cfg


def _build_sources_view(draft: "_wv2.WizardV2Draft") -> list[dict]:
    """Build the Step 3 sources list — only shows plugins that are
    ready to fetch (keyless OR credentials already in .env / os.environ),
    merged with whatever the LLM suggested.

    Shape per row:
      {plugin_id, display, rationale, stream_config, requires_key,
       ready, enabled, from_llm}

    Ordering:
      1. LLM-suggested + ready — first (usually pre-checked, with rationale)
      2. Other ready plugins — after (checkable, with generic rationale)
    LLM-suggested but NOT ready plugins are dropped: the user asked to hide
    sources that need config the admin hasn't done yet.
    """
    # Build a snapshot of what the user already enabled (or the LLM
    # suggested) so we can preserve toggles across page loads.
    suggested_by_id: dict[str, dict] = {}
    for s in (draft.suggested_sources or []):
        pid = s.get("plugin_id")
        if pid:
            suggested_by_id[pid] = s

    try:
        from sources.registry import get_registry
        reg = get_registry()
    except Exception:
        return list(suggested_by_id.values())

    import os
    from pathlib import Path as _Path
    env_snapshot = dict(os.environ)
    try:
        from dotenv import dotenv_values
        env_path = _Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            for k, v in (dotenv_values(env_path) or {}).items():
                if v:
                    env_snapshot.setdefault(k, v)
    except Exception:
        pass

    def _connection_ready(manifest) -> bool:
        required = [
            f for f in manifest.connection_fields
            if getattr(f, "required", False)
        ]
        if not required:
            return True
        return all(env_snapshot.get(f.name, "").strip() for f in required)

    def _required_stream_fields(manifest) -> list[str]:
        """Fields the plugin's fetch loop absolutely needs, per stream.
        We ignore the boilerplate `name` field (any string works; we
        generate one). Everything else that's `required=True` is a real
        per-stream config value the user has to provide."""
        return [f.name for f in manifest.stream_fields
                 if getattr(f, "required", False) and f.name != "name"]

    def _default_stream_config(plugin_id: str, manifest) -> dict:
        """Reasonable default stream config for plugins the LLM DIDN'T
        suggest. Only search-based sources get a meaningful auto-config
        (from the product display + aliases). Sources requiring an
        identifier the wizard doesn't know (feed_url, app_id, subreddit,
        etc.) return an incomplete config — those get filtered out by
        _stream_config_complete below rather than shown as broken rows."""
        display = draft.display or draft.slug
        cfg: dict = {}
        stream_field_names = {getattr(f, "name", "") for f in manifest.stream_fields}
        if "name" in stream_field_names:
            cfg["name"] = f"{plugin_id}-{draft.slug}"
        if "search_queries" in stream_field_names:
            queries = [display] + [a for a in (draft.aliases or []) if a]
            cfg["search_queries"] = queries
        if "query" in stream_field_names:
            cfg["query"] = display
        if "mode" in stream_field_names:
            cfg.setdefault("mode", "search")
        if "tags" in stream_field_names:
            cfg["tags"] = [
                t.lower().replace(" ", "")
                for t in ([display] + [a for a in (draft.aliases or []) if a])
            ]
        if "instance" in stream_field_names:
            cfg.setdefault("instance", "mastodon.social")
        return cfg

    def _stream_config_complete(manifest, cfg: dict) -> bool:
        """True when every required per-stream field is present + non-empty
        in `cfg`. If we can't tell (e.g. cfg is empty because we didn't
        auto-fill or the LLM's suggestion was incomplete), return False —
        the fetch would fail with "requires X" so hiding is better than
        surfacing a broken row."""
        for name in _required_stream_fields(manifest):
            val = cfg.get(name) if cfg else None
            if val in (None, "", []):
                return False
            if isinstance(val, list) and not any((v or "").strip() for v in val):
                return False
        return True

    def _describe_required_fields(manifest) -> list[dict]:
        """Metadata Step 3 renders as inline inputs when a required
        per-stream field isn't auto-fillable. Each entry has the field
        name, human label, help text, placeholder, and whether the value
        should be treated as a multi-line list (one stream per line)."""
        out: list[dict] = []
        for f in manifest.stream_fields:
            if not getattr(f, "required", False) or f.name == "name":
                continue
            # search_queries is textarea_list; the wizard already
            # auto-fills it from display + aliases, so no inline prompt.
            if f.name in ("search_queries", "query", "tags", "instance", "mode"):
                continue
            out.append({
                "name": f.name,
                "label": getattr(f, "label", f.name),
                "help": getattr(f, "help", "") or "",
                "placeholder": getattr(f, "placeholder", "") or "",
                # For text/number single-value fields, one line = one stream.
                # For csv fields, one line = one stream with those csv values.
                "type": getattr(f, "type", "text"),
                "one_per_line": getattr(f, "type", "text") in ("text", "number"),
            })
        return out

    def _existing_values(from_llm_cfg: dict, required_fields: list[dict]) -> dict[str, list[str]]:
        """Pre-populate the inline inputs with whatever the LLM (or the
        user on a prior save) has already provided. Values become a list
        of strings so the textarea renders one per line.

        Also picks up entries from `_extra_streams` — `_apply_inline_stream_config`
        splits a multi-line textarea into `cfg[field] = first` + a list of
        extra stream configs, so a round-trip through the configure form
        (save → back → forward, or save → re-open) needs the extras to be
        merged back into the field list. Without this, textareas re-render
        with only the first identifier and the extras stay orphaned in
        `_extra_streams` while looking gone from the operator's POV.
        """
        out: dict[str, list[str]] = {}
        extras = from_llm_cfg.get("_extra_streams") or []
        for f in required_fields:
            val = from_llm_cfg.get(f["name"])
            if val is None or val == "":
                collected: list[str] = []
            elif isinstance(val, list):
                collected = [str(v) for v in val if v]
            else:
                collected = [str(val)]
            # Append extras (deduping to preserve the round-trip invariant
            # even if the operator manually retyped a value that's also in
            # _extra_streams).
            seen = {v.strip().lower() for v in collected if isinstance(v, str)}
            for extra in extras:
                if not isinstance(extra, dict):
                    continue
                ev = extra.get(f["name"])
                if ev is None or ev == "":
                    continue
                if isinstance(ev, list):
                    for item in ev:
                        s = str(item).strip()
                        if s and s.lower() not in seen:
                            seen.add(s.lower())
                            collected.append(s)
                else:
                    s = str(ev).strip()
                    if s and s.lower() not in seen:
                        seen.add(s.lower())
                        collected.append(s)
            out[f["name"]] = collected
        return out

    view: list[dict] = []
    seen: set[str] = set()

    def _make_row(pid, plugin, is_from_llm, llm_source):
        stream_cfg = dict(llm_source.get("stream_config") or {}) if llm_source else {}
        if "name" not in stream_cfg:
            stream_cfg["name"] = f"{pid}-{draft.slug}"
        # For non-LLM sources, seed search_queries from display + aliases
        # (existing auto-fill behavior).
        if not is_from_llm:
            defaults = _default_stream_config(pid, plugin.manifest)
            for k, v in defaults.items():
                stream_cfg.setdefault(k, v)
        required_fields = _describe_required_fields(plugin.manifest)
        return {
            "plugin_id": pid,
            "display": plugin.manifest.display_name,
            "rationale": (llm_source.get("rationale") or "") if llm_source else (
                "Available (not suggested by the assistant for this "
                "product — enable if you want it too)."
            ),
            "stream_config": stream_cfg,
            "requires_key": False,
            # Nothing pre-checked on Step 3's pick screen — the user
            # explicitly opts in. `enabled` still tracks state across
            # saves (True after they check + save; back-navigation
            # preserves the toggles they picked).
            "enabled": bool(llm_source.get("enabled", False)) if is_from_llm else False,
            "from_llm": is_from_llm,
            # ADR-0021 taxonomy — used by the pick screen to group rows.
            "source_category": getattr(plugin.manifest, "source_category", "custom_source"),
            "content_types": list(getattr(plugin.manifest, "content_types", ["user_feedback"])),
            # ADR-0031 — first matching top-level content type; wizard pick
            # list shows the row once under this section.
            "primary_content_type": next(
                (ct for ct in ("user_feedback", "media_coverage")
                 if ct in getattr(plugin.manifest, "content_types", ["user_feedback"])),
                "user_feedback",
            ),
            # Fields the user has to type identifiers into inline.
            "required_stream_fields": required_fields,
            "field_values": _existing_values(stream_cfg, required_fields),
            "needs_inline_config": bool(required_fields),
        }

    # Pass 1 — LLM-suggested plugins that are ready.
    for pid, s in suggested_by_id.items():
        plugin = reg.get(pid)
        if plugin is None:
            continue  # Unknown plugin id.
        if not _connection_ready(plugin.manifest):
            continue  # Missing credentials.
        view.append(_make_row(pid, plugin, True, s))
        seen.add(pid)

    # Pass 2 — any other ready plugins the LLM didn't mention.
    for plugin in reg.all_plugins():
        pid = plugin.manifest.plugin_id
        if pid in seen:
            continue
        if getattr(plugin.manifest, "category", "source") != "source":
            continue
        if not _connection_ready(plugin.manifest):
            continue
        view.append(_make_row(pid, plugin, False, {}))

    return view


def _detect_provider_from_endpoint(endpoint: str) -> str:
    """Best-effort match: return provider id, or 'custom' if nothing matches."""
    if not endpoint:
        return ""
    e = endpoint.lower()
    for p in _LLM_WIZARD_PROVIDERS:
        pref = p["endpoint"].lower().rstrip("/")
        if e.startswith(pref) or pref in e:
            return p["id"]
    return "custom"


def _env_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".env"


def _env_has_key(name: str) -> bool:
    if not name:
        return True  # no key needed
    import os
    if os.environ.get(name, "").strip():
        return True
    try:
        from dotenv import dotenv_values
        p = _env_path()
        if p.exists():
            return bool((dotenv_values(p) or {}).get(name, "").strip())
    except Exception:
        pass
    return False


@router.get("/wizard/llm", response_class=HTMLResponse)
def llm_wizard(request: Request):
    """Standalone LLM setup wizard — always reachable, no flag gate."""
    cfg = _assistant_llm.current_config()
    current_provider = _detect_provider_from_endpoint(cfg.endpoint) if cfg else ""
    # Report readiness against the *assistant*-specific env var, not the
    # shared connections one — otherwise a user with only the pipeline key
    # set would see a misleading "key set" badge on the assistant wizard.
    # One env var powers the assistant across all providers, so readiness is
    # a single check — but we still key it per-provider so the UI can render
    # "no key needed" for Ollama.
    assistant_key_set = _env_has_key(ASSISTANT_LLM_API_KEY_ENV)
    return _render(
        request, "wizard/llm_setup.html",
        providers=_LLM_WIZARD_PROVIDERS,
        cfg=cfg,
        current_provider=current_provider,
        assistant_enabled=_features.enabled("assistant_llm_enabled"),
        assistant_key_env=ASSISTANT_LLM_API_KEY_ENV,
        assistant_key_present=assistant_key_set,
        return_url=request.query_params.get("return", ""),
        error=request.query_params.get("error", ""),
        notice=request.query_params.get("notice", ""),
        env_ready={
            p["id"]: (True if not p.get("needs_key", True) else assistant_key_set)
            for p in _LLM_WIZARD_PROVIDERS
        },
    )


@router.post("/wizard/llm")
async def llm_wizard_save(request: Request):
    """Save endpoint + model + optional API key. Enables the flag on success.

    Persists to three places atomically-enough:
    1. config/assistant_llm.yaml  — endpoint + model + tuning
    2. .env                        — provider API key (only if user supplied one)
    3. config/features.yaml        — flips assistant_llm_enabled=true
    """
    form = dict(await request.form())
    provider_id = (form.get("provider") or "").strip()
    endpoint = (form.get("endpoint") or "").strip()
    model = (form.get("model") or "").strip()
    api_key = (form.get("api_key") or "").strip()
    return_url = (form.get("return_url") or "").strip()

    provider = _providers_by_id().get(provider_id, {})
    # Fill in defaults from provider preset when the user didn't override.
    if not endpoint:
        endpoint = provider.get("endpoint", "")
    if not model:
        model = provider.get("default_model", "")
    if not endpoint or not model:
        return _llm_wizard_redirect(
            "endpoint+and+model+are+required", return_url,
        )

    # 1) Persist API key to .env when supplied. The assistant uses a SINGLE
    #    provider-agnostic env var (`ASSISTANT_LLM_API_KEY`) — switching
    #    providers overwrites the value. Distinct from the per-provider
    #    /connections/<provider> keys the pipeline-time relevance/classify
    #    stages read.
    if api_key and provider.get("needs_key", True) is False:
        # Local provider (Ollama etc.) — no key to persist, silently ignore.
        api_key = ""
    if api_key and not provider_id:
        # No provider card clicked; infer from the endpoint URL so we can
        # decide whether a key is even needed for this endpoint.
        detected = _detect_provider_from_endpoint(endpoint)
        provider = _providers_by_id().get(detected, {})
        if not provider:
            return _llm_wizard_redirect(
                "cannot+determine+provider+-+pick+one+above", return_url,
            )
        if not provider.get("needs_key", True):
            api_key = ""
    key_env = ASSISTANT_LLM_API_KEY_ENV if api_key else ""
    if api_key and key_env:
        try:
            # env_writer.set_var writes to .env AND syncs os.environ so
            # the just-saved key is visible to the next LLM call in this
            # process (no restart needed) — see pipeline/env_writer.py.
            from pipeline import env_writer
            env_writer.set_var(_env_path(), key_env, api_key)
        except Exception as e:
            return _llm_wizard_redirect(
                f"saving+api+key+failed:+{str(e)[:80]}", return_url,
            )

    # 2) Persist assistant_llm.yaml.
    try:
        cfg = _assistant_llm.AssistantLLMConfig(
            endpoint=endpoint, model=model,
            temperature=float(form.get("temperature", "0.2") or 0.2),
            seed=int(form["seed"]) if (form.get("seed") or "").strip() else None,
            timeout_seconds=int(form.get("timeout_seconds", "60") or 60),
            max_retries=int(form.get("max_retries", "3") or 3),
            budget_usd_per_product_per_month=float(
                form.get("budget_usd_per_product_per_month", "10.0") or 10.0,
            ),
            # The config records the single provider-agnostic env var name so
            # the loader knows exactly what to look up — even if the endpoint
            # URL later changes to a different provider.
            api_key_env=(
                ASSISTANT_LLM_API_KEY_ENV
                if provider.get("needs_key", True) else ""
            ),
        )
        _assistant_llm.save_config(cfg)
    except (ValueError, KeyError) as e:
        return _llm_wizard_redirect(f"config+invalid:+{str(e)[:80]}", return_url)

    # 3) Flip the feature flag on so the wizard drafting service can call it.
    _flip_feature_flag("assistant_llm_enabled", True)

    # Optional health probe on save — informational, non-blocking. Pass the
    # typed key directly since the .env write may not be visible to os.environ
    # in this process yet (dotenv only writes; it doesn't hot-reload the env).
    ok, msg = _probe_assistant_llm(endpoint, model, key_env, typed_key=api_key)
    notice = "saved" + ("+and+reachable" if ok else "+but+probe+failed:+" + str(msg)[:60])
    return _llm_wizard_redirect(None, return_url, notice=notice)


@router.post("/wizard/llm/test")
async def llm_wizard_test(request: Request):
    """Ajax-style health check that doesn't persist anything. Returns JSON.

    Uses the key from the form if the user just typed one — otherwise falls
    back to the env var. Without this, testing a *new* key before saving
    would probe with the stale (or missing) key and always fail.
    """
    form = dict(await request.form())
    provider_id = (form.get("provider") or "").strip()
    provider = _providers_by_id().get(provider_id, {})
    endpoint = (form.get("endpoint") or "").strip() or provider.get("endpoint", "")
    model = (form.get("model") or "").strip() or provider.get("default_model", "")
    # Probe against ASSISTANT_LLM_API_KEY (not the connections key) so a
    # stale connections key can't accidentally make a broken assistant
    # setup look healthy. Local providers have needs_key=False → key_env="".
    needs_key = provider.get("needs_key", True)
    if not provider_id:
        # No card clicked — decide based on the endpoint URL.
        detected = _detect_provider_from_endpoint(endpoint)
        needs_key = _providers_by_id().get(detected, {}).get("needs_key", True)
    key_env = ASSISTANT_LLM_API_KEY_ENV if needs_key else ""
    typed_key = (form.get("api_key") or "").strip()
    ok, msg = _probe_assistant_llm(endpoint, model, key_env, typed_key=typed_key)
    return {"ok": ok, "message": msg}


def _llm_wizard_redirect(error: Optional[str], return_url: str,
                          notice: str = "") -> RedirectResponse:
    """Redirect back to the LLM wizard with a status query param.
    When the caller supplied `return_url` and the save was OK, redirect
    there instead so the product wizard can pick up right where it was."""
    if not error and return_url:
        return RedirectResponse(url=return_url, status_code=303)
    params = []
    if error:
        params.append(f"error={error}")
    if notice:
        params.append(f"notice={notice}")
    if return_url:
        from urllib.parse import quote
        params.append(f"return={quote(return_url, safe='')}")
    url = "/wizard/llm"
    if params:
        url += "?" + "&".join(params)
    return RedirectResponse(url=url, status_code=303)


def _flip_feature_flag(name: str, value: bool) -> None:
    """Toggle one flag in config/features.yaml. Idempotent."""
    import yaml as _yaml
    from pipeline.config import CONFIG_DIR
    p = CONFIG_DIR / "features.yaml"
    try:
        data = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    flags = data.get("features") or {}
    if flags.get(name) == value:
        _features.clear_cache()
        return
    flags[name] = value
    data["features"] = flags
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(_yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
                   encoding="utf-8")
    tmp.replace(p)
    _features.clear_cache()


def _key_diagnostic(api_key: str) -> str:
    """A single-line description of the key that doesn't leak the secret.

    Includes length + a redacted preview (`sk-a…8f2c` shape) so the user can
    tell at a glance whether the paste succeeded or got truncated. Length
    is the most useful signal: Anthropic keys are ~100+ chars, OpenAI ~50+,
    Gemini ~40+. A 12-char paste is almost certainly a truncation.
    """
    if not api_key:
        return "empty"
    n = len(api_key)
    if n <= 10:
        return f"{n} chars — probably truncated"
    return f"{n} chars ({api_key[:4]}…{api_key[-4:]})"


def _normalize_user_url(text: str) -> str:
    """Turn a Screen-1 `url_or_description` value into a usable URL, or "".

    Users type bare domains (`vapi.ai`, `notion.so`) as often as full URLs.
    Without normalization the profile page would show an empty URL field
    even though the wizard's fetcher had already retrieved the page. Rule:
      - full URL (http/https) → keep as-is (with whitespace trimmed)
      - single-token with a dot (`vapi.ai`) → prefix `https://`
      - anything else (freeform description text) → "" so we don't
        pollute the URL field with prose.
    """
    t = (text or "").strip()
    if not t:
        return ""
    low = t.lower()
    if low.startswith(("http://", "https://")):
        return t
    # Bare-domain heuristic: one token, contains a dot, no whitespace.
    if " " not in t and "\n" not in t and "\t" not in t and "." in t:
        return "https://" + t
    return ""


def _normalize_endpoint(endpoint: str) -> str:
    """Ensure the OpenAI SDK's `httpx.URL.join()` preserves the `/v1` path.

    Without a trailing slash, `URL("https://x/v1").join("chat/completions")`
    resolves to `https://x/chat/completions` (RFC 3986 relative-reference
    behavior — the last path segment is treated as a filename and replaced).
    Anthropic's OpenAI-compat docs mandate the trailing slash for this
    reason; we add it unconditionally so users don't have to remember.
    """
    e = (endpoint or "").strip()
    if e and not e.endswith("/"):
        e += "/"
    return e


def _probe_assistant_llm(endpoint: str, model: str, key_env: str,
                          *, typed_key: str = "") -> tuple[bool, str]:
    """Cheap health check that constructs the client directly (bypassing
    per-product routing) and makes a minimal chat-completions call.

    Why chat.completions and not models.list(): providers with an
    OpenAI-compat layer (Anthropic, Gemini) only forward
    `/chat/completions` and `/completions`. Their native `/v1/models`
    endpoint uses provider-specific auth headers (`x-api-key` for
    Anthropic) that the OpenAI SDK doesn't send — so `client.models.list()`
    returned 401 "Invalid bearer token" even with a perfectly valid key.
    `chat.completions.create(..., max_tokens=1)` exercises the actual
    path our real pipeline uses.

    Key resolution order (matters because `dotenv.set_key` writes the file
    but does NOT push into the running process's `os.environ`):
      (1) `typed_key` from the form   — user just pasted, not yet saved
      (2) `.env` on disk              — the file our Save just wrote to
      (3) `os.environ`                — shell exports or system env
    """
    api_key = ""
    source = ""
    try:
        import os
        from openai import OpenAI
        api_key = typed_key or ""
        source = "form" if api_key else ""
        if not api_key and key_env:
            try:
                from dotenv import dotenv_values
                api_key = (dotenv_values(_env_path()) or {}).get(key_env) or ""
                if api_key:
                    source = ".env"
            except Exception:
                pass
        if not api_key and key_env:
            api_key = os.environ.get(key_env) or ""
            if api_key:
                source = "os.environ"
        if key_env and not api_key:
            return False, f"no key — paste one above or set {key_env} in .env"
        client = OpenAI(
            base_url=_normalize_endpoint(endpoint),
            api_key=api_key or "dummy",
            timeout=15,
        )
        # Minimal round-trip: 1 completion token, one-word system+user prompt.
        # Confirms auth + endpoint + model in a single hop.
        client.chat.completions.create(
            model=model,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True, f"endpoint reachable (key from {source or 'none'})"
    except Exception as e:
        msg = str(e)
        src_hint = f" [key source: {source}]" if source else ""
        diag = f" [key: {_key_diagnostic(api_key)}]" if api_key else ""
        low = msg.lower()
        if "401" in msg or "invalid bearer" in low or "authentication" in low:
            return False, (
                f"401 — key rejected{src_hint}{diag}. "
                f"Verify the key in your provider's console. If length looks "
                f"right, the key may have been revoked or is for a different "
                f"project/workspace."
            )
        if "404" in msg or "not found" in low:
            return False, (
                f"404 — endpoint or model unrecognized. Endpoint: "
                f"{_normalize_endpoint(endpoint)}; model: {model}. ({msg[:100]})"
            )
        if "model" in low and ("not_found" in low or "unknown" in low or "invalid" in low):
            return False, f"model {model!r} not accepted by this endpoint. ({msg[:120]})"
        return False, (msg[:200] + src_hint + diag)


# ---------------------------------------------------------------------------
# Screen 1 (Describe) + landing
# ---------------------------------------------------------------------------


def _assistant_llm_status() -> dict:
    """Diagnose the assistant-LLM readiness in detail so the wizard's
    "set up your LLM" banner can point at the exact fix.

    Busts the features cache first: `_global_features` is `@lru_cache`d
    and if the flag was flipped by a different process (or a hand-edit
    of features.yaml) our current process still sees the stale value —
    leading to a "not configured" banner even after the admin set
    everything up correctly. Clearing on every landing render is cheap.
    """
    _features.clear_cache()
    configured = _assistant_llm.is_configured()
    enabled = _features.enabled("assistant_llm_enabled")
    ready = configured and enabled
    if configured and not enabled:
        reason = "flag_off"
        detail = ("The <code>assistant_llm_enabled</code> flag is off. "
                   "Enable it on the Features tab to use the assistant "
                   "for drafting.")
    elif not configured and enabled:
        reason = "config_missing"
        detail = ("The <code>config/assistant_llm.yaml</code> file is "
                   "missing endpoint or model. Set them via "
                   "<a href=\"/wizard/llm\">/wizard/llm</a> or the "
                   "Connections tab.")
    elif not configured:
        reason = "config_and_flag"
        detail = ("The assistant LLM isn't configured and its flag is off. "
                   "Run the guided setup at "
                   "<a href=\"/wizard/llm\">/wizard/llm</a>.")
    else:
        reason = ""
        detail = ""
    return {"ready": ready, "reason": reason, "detail": detail}


@router.get("/wizard", response_class=HTMLResponse)
def wizard_landing(request: Request):
    _require_flag()
    drafts = _wv2.list_drafts(_products_dir())
    status = _assistant_llm_status()
    # Landing page shows every draft in the "Resume a draft" list.
    # Fields render blank — operator hit /wizard directly, not via a
    # specific draft, so we don't guess which one they meant to
    # resume. Multi-draft installs work naturally: pick from the list.
    return _render(
        request, "wizard/step_describe.html",
        drafts=drafts,
        resume_draft=None,
        valid_goals=VALID_GOALS,
        existing_products=available_products(),
        error=request.query_params.get("error", ""),
        assistant_llm_ready=status["ready"],
        assistant_llm_status=status,
    )


@router.post("/wizard/draft")
def wizard_create_draft(
    request: Request,
    display: str = Form(...),
    url_or_description: str = Form(""),
    include_competition: bool = Form(default=False),
):
    """Create a v2 draft from Screen-1 form input, then run ProfileDraft.

    Screen 1 collects: product name + optional URL/description + one
    preference (Include competition analysis?). Downstream `goals` list
    stays on the draft as an empty list — profile_draft accepts an empty
    goals list transparently.

    Redirect on success straight to the /wizard/{slug} step page (which
    renders the profile screen). On name/slug collision, redirect back to
    the landing page with an ?error= message.
    """
    _require_flag()
    display = (display or "").strip()
    if not display:
        return RedirectResponse(url="/wizard?error=name+is+required", status_code=303)
    slug = _wv2.slugify(display)
    products_dir = _products_dir()

    # Collision with an existing product (already-configured slug) — hard
    # error, we don't overwrite live products.
    if (products_dir / slug).is_dir():
        return RedirectResponse(
            url=f"/wizard?error=slug+{slug}+already+used+by+a+product",
            status_code=303,
        )

    # Existing DRAFT with the same slug — update-in-place instead of the
    # earlier silent redirect. The operator went back to describe, edited
    # the fields, and re-clicked "Draft my profile" expecting the profile
    # to redraft with the new inputs. Overwrite display / url / include_competition
    # from the form, then re-run draft_profile so the profile screen
    # reflects the update.
    existing = _wv2.load_draft(products_dir, slug)
    url_norm = (url_or_description or "").strip()
    if existing is not None:
        existing.display = display
        existing.url_or_description = url_norm
        existing.include_competition = bool(include_competition)
        existing.step = "describe"
        _wv2.save_draft(products_dir, existing)
        result = _profile_draft.draft_profile(
            display, url_norm, [],
            product_id_for_budget=slug,
        )
        existing.page_fetch_failed = result.page_fetch_failed
        existing.fetched_chars = result.fetched_chars
        existing.drafting_error = result.error_message
        existing.url = _normalize_user_url(url_norm)
        _wv2.apply_profile_draft(existing, result.profile)
        existing.step = "profile"
        _wv2.save_draft(products_dir, existing)
        return RedirectResponse(url=f"/wizard/{slug}", status_code=303)

    draft = _wv2.WizardV2Draft(
        slug=slug, display=display, step="describe",
        url_or_description=url_norm,
        goals=[],
        include_competition=bool(include_competition),
    )
    _wv2.save_draft(products_dir, draft)

    result = _profile_draft.draft_profile(
        display, url_or_description or "", [],
        product_id_for_budget=slug,
    )
    draft.page_fetch_failed = result.page_fetch_failed
    draft.fetched_chars = result.fetched_chars
    draft.drafting_error = result.error_message
    draft.url = _normalize_user_url(url_or_description or "")
    _wv2.apply_profile_draft(draft, result.profile)
    draft.step = "profile"
    _wv2.save_draft(products_dir, draft)

    return RedirectResponse(url=f"/wizard/{slug}", status_code=303)


# ---------------------------------------------------------------------------
# Screen dispatch (state machine)
# ---------------------------------------------------------------------------


@router.get("/wizard/{slug}", response_class=HTMLResponse)
def wizard_step(request: Request, slug: str):
    _require_flag()
    draft = _get_draft_or_404(slug)

    # If the draft still carries a stale "assistant LLM not configured" error
    # from an earlier visit and the LLM is now available, auto-retry drafting
    # once so the user isn't stuck looking at a banner that no longer applies.
    if (draft.drafting_error
            and "assistant LLM not configured" in draft.drafting_error
            and _assistant_llm.is_configured()
            and _features.enabled("assistant_llm_enabled")):
        try:
            result = _profile_draft.draft_profile(
                draft.display, draft.url_or_description, list(draft.goals),
                product_id_for_budget=draft.slug,
            )
            if result.profile is not None:
                _wv2.apply_profile_draft(draft, result.profile)
                draft.drafting_error = ""
                draft.page_fetch_failed = result.page_fetch_failed
                _wv2.save_draft(_products_dir(), draft)
            else:
                # Even if drafting still fails, refresh the reason so the
                # banner matches reality (may now be a network / probe issue,
                # not "not configured").
                if "assistant LLM not configured" not in (result.error_message or ""):
                    draft.drafting_error = result.error_message
                    _wv2.save_draft(_products_dir(), draft)
        except Exception:
            pass

    ctx = {
        "draft": draft,
        "valid_goals": VALID_GOALS,
        "regen_sections": _wv2.REGEN_SECTIONS,
        "regen_cap": _wv2.MAX_REGENERATIONS_PER_SECTION,
        "error": request.query_params.get("error", ""),
        "notice": request.query_params.get("notice", ""),
    }
    step = draft.step or "describe"
    if step == "profile":
        return _render(request, "wizard/step_profile.html", **ctx)
    if step == "sources":
        # Two sub-phases within Step 3:
        #   pick      — checklist of ready sources, none selected by default
        #   configure — for each SELECTED source that needs a per-stream
        #               identifier, show the plain constrained textarea
        #               (one value per line). No LLM suggestions.
        substep = draft.sources_substep or "pick"
        if substep == "configure":
            # Fill empty identifier fields (e.g. stackex site) via
            # discover_streams. Idempotent — skips streams that already
            # have values. Also covers operators who landed on configure
            # before a plugin gained discovery support.
            _prepopulate_via_discovery(draft)
            _wv2.save_draft(_products_dir(), draft)
        sources_view = _build_sources_view(draft)
        # Media coverage sites are the curated feed list from
        # config/media_sources.yaml — the "Media Coverage Sources" pick
        # (rss plugin) expands into a subset picker over these entries in
        # the configure sub-step. Names alone are surfaced on the pick
        # screen as a "Covers:" preview.
        try:
            from pipeline import media_sources as _media_sources
            media_catalog = list(_media_sources.load())
        except Exception:
            media_catalog = []
        media_source_names = [m["name"] for m in media_catalog]
        # Which catalog URLs are currently picked on the draft's rss stream
        # (base + _extra_streams). Feeds the checkbox pre-check state on
        # the configure sub-step.
        catalog_picked: set[str] = set()
        for s in (draft.suggested_sources or []):
            if s.get("plugin_id") != "rss":
                continue
            cfg = s.get("stream_config") or {}
            if cfg.get("feed_url"):
                catalog_picked.add(cfg["feed_url"])
            for extra in (cfg.get("_extra_streams") or []):
                if isinstance(extra, dict) and extra.get("feed_url"):
                    catalog_picked.add(extra["feed_url"])
        ctx.update({
            "sources_view": sources_view,
            "sources_substep": substep,
            "n_enabled": sum(1 for s in sources_view if s.get("enabled")),
            "media_source_names": media_source_names,
            "media_catalog": media_catalog,
            "catalog_picked_urls": catalog_picked,
        })
        return _render(request, "wizard/step_sources.html", **ctx)
    if step == "calibrate":
        status = _minifetch.read_status(draft.slug)
        judged = (draft.calibration or {}).get("judgments") or {}
        deck = _minifetch.sample_deck(
            draft.slug, size=10, exclude_ids=judged.keys(),
        ) if status.status == _minifetch.STATUS_READY else []
        ctx.update({
            "minifetch_status": status,
            "deck": deck,
            "n_judged": len(judged),
            "min_useful": _minifetch.MIN_USEFUL_ITEMS,
        })
        return _render(request, "wizard/step_calibrate.html", **ctx)
    if step == "review":
        judgments = (draft.calibration or {}).get("judgments") or {}
        n_pos = sum(1 for v in judgments.values() if v.get("polarity") == "positive_example")
        n_neg = sum(1 for v in judgments.values() if v.get("polarity") == "negative_example")
        ctx.update({
            "n_positive_snippets": n_pos,
            "n_negative_snippets": n_neg,
            "n_active_sources": sum(1 for s in draft.suggested_sources if s.get("enabled")),
            "n_locked_sources": sum(
                1 for s in draft.suggested_sources
                if s.get("requires_key") and not s.get("enabled")
            ),
            "cost_estimate": _cost_estimate(draft),
            "provider_presets": _provider_presets(),
            "llm_options": _available_llm_options(),
        })
        return _render(request, "wizard/step_review.html", **ctx)
    # An existing draft that's on the describe step must ALSO get the
    # assistant-LLM readiness check — otherwise the banner shows
    # unconditionally on step_describe (undefined jinja var = falsy).
    # `resume_draft=draft` pre-fills the display + url fields with THIS
    # draft's values — the user is resuming this specific draft via
    # /wizard/{slug}, not landing on /wizard where multiple drafts
    # might be in flight. Multi-draft-in-flight installs work: pick
    # from the resume list on /wizard, then this handler renders the
    # picked draft's fields pre-filled.
    status = _assistant_llm_status()
    return _render(request, "wizard/step_describe.html",
                   drafts=[draft],
                   resume_draft=draft,
                   valid_goals=VALID_GOALS,
                   existing_products=available_products(),
                   error=ctx["error"],
                   assistant_llm_ready=status["ready"],
                   assistant_llm_status=status)


# ---------------------------------------------------------------------------
# Screen 2 (Profile) — save edits + per-section regen
# ---------------------------------------------------------------------------


def _split_lines_field(value: str) -> list[str]:
    """Chip-editor UI submits one item per line. Strip + dedupe."""
    if not value:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for ln in value.splitlines():
        s = ln.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


@router.post("/wizard/{slug}/profile")
async def wizard_save_profile(slug: str, request: Request):
    """Persist edits from Screen 2. Form fields are one-per-line textareas
    (from the shared `_chips.html` partial)."""
    _require_flag()
    draft = _get_draft_or_404(slug)
    form = await request.form()

    draft.description = (form.get("description") or "").strip()
    draft.url = (form.get("url") or "").strip()
    draft.aliases = _split_lines_field(form.get("aliases") or "")
    draft.not_to_be_confused_with = _split_lines_field(
        form.get("not_to_be_confused_with") or "",
    )
    draft.competitors = _split_lines_field(form.get("competitors") or "")
    draft.scope_in = _split_lines_field(form.get("scope_in") or "")
    draft.scope_out = _split_lines_field(form.get("scope_out") or "")
    # include_competition is collected on Screen 1 (describe) — this
    # handler intentionally does not re-read it.

    # Suggested-source enable toggles: form contains src_enabled=<plugin_id>
    # for each enabled item; anything absent stays as-is except the enabled
    # Suggested-source toggles live on the dedicated Sources step now, but
    # legacy forms (or a merged review pattern) may still submit them here.
    if "src_enabled" in form:
        enabled_ids = set(form.getlist("src_enabled"))
        for src in draft.suggested_sources:
            src["enabled"] = src.get("plugin_id") in enabled_ids

    action = (form.get("action") or "").strip()
    if action == "advance":
        draft.step = "sources"
    _wv2.save_draft(_products_dir(), draft)
    return RedirectResponse(url=f"/wizard/{slug}", status_code=303)


@router.post("/wizard/{slug}/sources")
async def wizard_save_sources(slug: str, request: Request):
    """Persist source toggles from Screen 3 (Choose sources).

    Actions:
      save     — save toggles and stay on the sources step
      advance  — save toggles, start mini-fetch, jump to calibrate step
    """
    _require_flag()
    draft = _get_draft_or_404(slug)
    form = await request.form()
    action = (form.get("action") or "save").strip()
    substep = draft.sources_substep or "pick"

    # ------------------------------------------------------------------
    # PICK phase — the user is choosing which sources to include.
    # No per-stream config fields on this form; just checkboxes.
    # ------------------------------------------------------------------
    if substep == "pick":
        enabled_ids = set(form.getlist("src_enabled"))
        existing_by_id = {s.get("plugin_id"): s for s in draft.suggested_sources}

        # Refresh requires_key from live connection readiness. Drafts may
        # carry a stale True from older annotation that treated optional
        # secrets (STACKEX_KEY) as required — that locked the toggle off
        # even when the plugin was shown on the pick list.
        view = _build_sources_view(draft)
        view_by_id = {v["plugin_id"]: v for v in view}
        ready_ids = set(view_by_id.keys())

        for src in draft.suggested_sources:
            pid = src.get("plugin_id")
            src["requires_key"] = pid not in ready_ids
            if src["requires_key"]:
                src["enabled"] = False
            else:
                src["enabled"] = pid in enabled_ids

        # User checked a plugin that wasn't in the LLM's suggestions.
        # Materialize it into draft.suggested_sources so downstream code
        # (materialize, minifetch) treats it uniformly.
        for pid in enabled_ids:
            if pid in existing_by_id:
                continue
            v = view_by_id.get(pid)
            if v is None:
                continue
            draft.suggested_sources.append({
                "plugin_id": pid,
                "rationale": v.get("rationale", ""),
                "stream_config": v.get("stream_config") or {},
                "requires_key": False,
                "enabled": True,
            })

        if action == "advance":
            # Move to the configure phase. Pre-populate identifiers for
            # sources that implement discover_streams (ADR-0030) so the
            # configure form's textarea comes pre-filled — user reviews
            # + edits instead of typing from scratch.
            _prepopulate_via_discovery(draft)
            draft.sources_substep = "configure"
        _wv2.save_draft(_products_dir(), draft)
        return RedirectResponse(url=f"/wizard/{slug}", status_code=303)

    # ------------------------------------------------------------------
    # CONFIGURE phase — for each SELECTED source that needs per-stream
    # identifiers, the user picks from suggestions + adds typed values.
    # ------------------------------------------------------------------
    # Build the view with the enabled state persisted from the pick phase.
    view = _build_sources_view(draft)
    view_by_id = {v["plugin_id"]: v for v in view}
    _apply_inline_stream_config(draft, form, view_by_id)

    if action == "back":
        draft.sources_substep = "pick"
        _wv2.save_draft(_products_dir(), draft)
        return RedirectResponse(url=f"/wizard/{slug}", status_code=303)

    if action == "advance":
        # Reset for future back-navigation so the user starts on pick
        # again if they come back to Step 3.
        draft.sources_substep = "pick"
        # Slice A/C/E — pass profile facts so minifetch can keyword-gate
        # + alias-widen HN queries, and honor the optional LLM-gate toggle.
        _minifetch.start_minifetch(
            draft.slug,
            list(draft.suggested_sources),
            product_facts=_draft_product_facts(draft),
            use_llm_gate=(form.get("use_llm_gate") in ("1", "on", "true")),
        )
        draft.step = "calibrate"
        if not draft.calibration:
            draft.calibration = {"judgments": {}}
    _wv2.save_draft(_products_dir(), draft)
    return RedirectResponse(url=f"/wizard/{slug}", status_code=303)


@router.post("/wizard/{slug}/back")
def wizard_step_back(slug: str):
    """Move the draft one step earlier so the user can revisit past choices.
    Never leaves the state machine — silently no-ops if we're already on
    the first step."""
    _require_flag()
    draft = _get_draft_or_404(slug)
    prev = _wv2.previous_step(draft.step or "describe")
    if prev is not None:
        draft.step = prev
        _wv2.save_draft(_products_dir(), draft)
    return RedirectResponse(url=f"/wizard/{slug}", status_code=303)


@router.post("/wizard/{slug}/regen/{section}")
async def wizard_regenerate_section(slug: str, section: str, request: Request):
    """Re-draft the whole profile and overwrite one section on the draft.

    Because the regen buttons live INSIDE the profile edit form (via
    `<button formaction="...">`), the parent form's data is submitted
    with the regen POST. We take that as a save-and-then-regen: preserve
    the user's in-flight edits on other sections, THEN overwrite the
    target section with the LLM's fresh draft. Without this, clicking
    Regenerate would silently roll back everything the user edited on
    other sections since the last full Save.

    Cap at MAX_REGENERATIONS_PER_SECTION per section (existing D-series rule).
    """
    _require_flag()
    if section not in _wv2.REGEN_SECTIONS:
        raise HTTPException(status_code=400, detail=f"unknown section {section!r}")
    draft = _get_draft_or_404(slug)
    if not draft.can_regenerate(section):
        return RedirectResponse(
            url=f"/wizard/{slug}?error=regen+cap+reached+for+{section}#section-{section}",
            status_code=303,
        )

    # Persist any in-flight edits from the parent profile form first. Skip
    # only the section the user is regenerating — that value would be stale
    # anyway. If the form is empty (regen button clicked outside the profile
    # step), the form fields are just missing and we leave the draft as-is.
    try:
        form = await request.form()
    except Exception:
        form = None
    if form:
        _apply_form_to_draft(draft, form, skip_section=section)

    result = _profile_draft.draft_profile(
        draft.display, draft.url_or_description, list(draft.goals),
        product_id_for_budget=draft.slug,
    )
    if result.profile is None:
        # Save whatever edits we captured from the form even if the LLM
        # call failed — losing those on error would be a bad surprise.
        _wv2.save_draft(_products_dir(), draft)
        return RedirectResponse(
            url=(
                f"/wizard/{slug}?error={result.error_message or 'regen+failed'}"
                f"#section-{section}"
            ),
            status_code=303,
        )
    _wv2.apply_profile_draft(draft, result.profile, only_section=section)
    draft.note_regeneration(section)
    _wv2.save_draft(_products_dir(), draft)
    # Fragment preserves scroll position — after the redirect the browser
    # jumps to the regenerated section instead of scrolling to the top.
    return RedirectResponse(
        url=f"/wizard/{slug}?notice=regenerated+{section}#section-{section}",
        status_code=303,
    )


def _apply_form_to_draft(draft: "_wv2.WizardV2Draft", form, *, skip_section: str = "") -> None:
    """Copy Screen-2 form fields into the draft. Only overwrites fields the
    form actually declares (missing keys leave the draft untouched) so this
    is safe to call from routes that share the same form layout.

    `skip_section` names one field to leave alone — used by the regen path
    where the target section will be filled by the LLM afterwards.
    """
    def _lines(name: str) -> list[str]:
        raw = form.get(name)
        if raw is None:
            return None  # signal "not present in this form"
        out, seen = [], set()
        for ln in (raw or "").splitlines():
            s = ln.strip()
            if s and s not in seen:
                seen.add(s); out.append(s)
        return out

    if "description" in form and skip_section != "description":
        draft.description = (form.get("description") or "").strip()
    if "url" in form and skip_section != "url":
        draft.url = (form.get("url") or "").strip()
    for chip_field in ("aliases", "not_to_be_confused_with", "competitors",
                       "scope_in", "scope_out"):
        if chip_field == skip_section:
            continue
        parsed = _lines(chip_field)
        if parsed is not None:
            setattr(draft, chip_field, parsed)
    # Source enable toggles — respect current selection when the form
    # submits them. Absent field means "outside the profile step", leave alone.
    if "src_enabled" in form and skip_section != "suggested_sources":
        enabled_ids = set(form.getlist("src_enabled"))
        for src in draft.suggested_sources:
            src["enabled"] = src.get("plugin_id") in enabled_ids


@router.post("/wizard/{slug}/discard")
async def wizard_discard(slug: str, request: Request):
    """Delete the draft + its temp corpus. The `return_url` form field lets
    the caller stay where they were — the home page's Discard button posts
    `return_url=/` so the user isn't yanked into the /wizard first-run flow
    when they only wanted to delete one draft. Anything else defaults to
    /wizard so wizard-internal Discard buttons keep their historic behavior."""
    _require_flag()
    _minifetch.discard_corpus(slug)
    _wv2.discard_draft(_products_dir(), slug)
    form = await request.form()
    return_url = (form.get("return_url") or "").strip()
    # Whitelist to prevent open-redirect: allowed paths only.
    if return_url in ("/", "/wizard") or return_url.startswith("/products/"):
        target = return_url
    else:
        target = "/wizard"
    return RedirectResponse(url=target, status_code=303)


# ---------------------------------------------------------------------------
# Screen 3 (Calibrate) — mini-fetch + judgment deck
# ---------------------------------------------------------------------------


@router.post("/wizard/{slug}/minifetch")
async def wizard_start_minifetch(slug: str, request: Request):
    """Kick off a background mini-fetch. Renders the calibrate step, which
    polls status until READY / EMPTY."""
    _require_flag()
    draft = _get_draft_or_404(slug)
    form = await request.form()
    _minifetch.start_minifetch(
        draft.slug,
        list(draft.suggested_sources),
        product_facts=_draft_product_facts(draft),
        use_llm_gate=(form.get("use_llm_gate") in ("1", "on", "true")),
    )
    draft.step = "calibrate"
    if not draft.calibration:
        draft.calibration = {"judgments": {}}
    _wv2.save_draft(_products_dir(), draft)
    return RedirectResponse(url=f"/wizard/{slug}", status_code=303)


def _draft_product_facts(draft) -> dict:
    """Extract the fields that pipeline.minifetch's keyword + LLM gates need.

    Kept minimal — display + aliases drive the keyword pass; scope/description
    are only used by the optional LLM pass (Slice E). All fields are
    normalized to strings/lists so downstream code doesn't have to defend
    against WizardV2Draft's flexible shapes.
    """
    def _clean_list(raw) -> list[str]:
        if not raw:
            return []
        out: list[str] = []
        for v in raw:
            s = str(v).strip() if v is not None else ""
            if s:
                out.append(s)
        return out
    return {
        "display": (draft.display or draft.slug or "").strip(),
        "description": (draft.description or "").strip(),
        "url": (getattr(draft, "url", None) or getattr(draft, "url_or_description", None) or "").strip(),
        "aliases": _clean_list(draft.aliases),
        "scope_in": _clean_list(draft.scope_in),
        "scope_out": _clean_list(draft.scope_out),
    }


def _prepopulate_via_discovery(draft) -> None:
    """Run `Source.discover_streams()` (ADR-0030) for each enabled
    source and pre-populate the draft's stream_config with the
    provider-validated candidates.

    Called at the pick → configure transition in wizard Step 3. The
    identifier field on each source's stream_config becomes a LIST of
    values; the configure form's textarea then renders one-per-line
    and the user reviews + edits before advancing to calibrate.

    Every outcome logs at INFO/WARNING so an operator seeing an empty
    textarea can `tail -f` uvicorn's output and find out why.
    Failures are non-fatal — the operator still gets the empty textarea
    and can type identifiers manually.

    Only fires when the target field is currently empty. If the user
    has already typed values and back-navigated, we don't clobber them.
    """
    import logging as _logging
    _log = _logging.getLogger("wizard.discovery")
    from sources.base import Source as _BaseSource
    try:
        from sources.registry import get_registry
        reg = get_registry()
    except Exception as e:
        _log.warning("registry_load_failed: %s", e)
        return
    if reg is None:
        _log.warning("registry_missing")
        return

    profile = _draft_product_facts(draft)
    # Budget attribution for assistant LLM calls inside discover_streams.
    profile["product_id"] = draft.slug
    profile["slug"] = draft.slug

    for src in draft.suggested_sources:
        if not src.get("enabled"):
            continue
        pid = src.get("plugin_id") or ""
        plugin = reg.get(pid)
        if plugin is None:
            _log.info("skip: %s not in registry", pid)
            continue
        source_cls = getattr(plugin, "source_cls", None)
        # Skip plugins that use the default discover_streams (returns []).
        if (
            source_cls is None
            or getattr(source_cls, "discover_streams", None) is _BaseSource.discover_streams
        ):
            _log.info("skip: %s uses default discover_streams (no impl)", pid)
            continue
        id_field = getattr(plugin.manifest, "identifier_field", "") or ""
        if not id_field:
            _log.info("skip: %s has no identifier_field", pid)
            continue
        cfg = dict(src.get("stream_config") or {})
        # Skip if the operator already has values in the identifier field.
        existing = cfg.get(id_field)
        if isinstance(existing, list) and any((v or "").strip() for v in existing):
            _log.info("skip: %s already has %d %s values", pid, len(existing), id_field)
            continue
        if isinstance(existing, str) and existing.strip():
            _log.info("skip: %s already has scalar %s", pid, id_field)
            continue

        try:
            source = source_cls()
        except Exception as e:
            _log.warning("skip: %s can't instantiate (%s) — check credentials", pid, e)
            continue

        try:
            candidates = source.discover_streams(profile)
        except Exception as e:
            _log.warning("%s.discover_streams raised: %s", pid, e)
            continue

        if not candidates:
            _log.warning(
                "%s.discover_streams returned zero candidates. "
                "Likely causes: assistant LLM unconfigured/unreachable, "
                "provider search returned empty, or plugin credentials "
                "not set (praw for reddit). Check /connections/assistant_llm.",
                pid,
            )
            continue

        # Take each candidate's identifier and stack into the field as
        # a list. `_existing_values()` renders lists one-per-line.
        values: list[str] = []
        for c in candidates:
            v = c.stream_config.get(id_field)
            if isinstance(v, str) and v.strip():
                values.append(v.strip())
        if values:
            cfg[id_field] = values
            src["stream_config"] = cfg
            _log.info("pre-populated %s with %d %s values", pid, len(values), id_field)


@router.get("/wizard/{slug}/minifetch/status")
def wizard_minifetch_status(slug: str):
    """JSON endpoint for the poll loop on the calibrate screen."""
    _require_flag()
    _get_draft_or_404(slug)
    return _minifetch.read_status(slug).to_dict()


_VERDICT_TO_POLARITY = {
    "relevant": "positive_example",
    "not_relevant": "negative_example",
}


@router.post("/wizard/{slug}/calibrate/done")
def wizard_calibrate_finish(slug: str):
    """User declared calibration complete → propose a taxonomy from the corpus
    + confirmed facts and advance to the review step.

    Registered BEFORE the item-id variant below so the literal ``done`` isn't
    swallowed by ``{item_id}`` — FastAPI matches routes in declaration order.
    """
    _require_flag()
    draft = _get_draft_or_404(slug)
    corpus = _minifetch.load_corpus(slug)
    proposal, grounded = _tax_prop.propose_taxonomy(
        profile_facts={
            "display": draft.display,
            "description": draft.description,
            "aliases": draft.aliases,
            "scope_in": draft.scope_in,
            "scope_out": draft.scope_out,
            "competitors": draft.competitors,
        },
        corpus=corpus,
        product_id_for_budget=draft.slug,
    )
    if proposal is not None:
        from datetime import date
        draft.proposed_taxonomy = _tax_prop.to_taxonomy_yaml(
            proposal, version=date.today().isoformat(),
        )
        draft.proposed_taxonomy["_grounded"] = grounded
    draft.step = "review"
    _wv2.save_draft(_products_dir(), draft)
    return RedirectResponse(url=f"/wizard/{slug}", status_code=303)


@router.post("/wizard/{slug}/calibrate")
async def wizard_calibrate_item(slug: str, request: Request):
    """Record one judgment on the draft. `verdict` is relevant|not_relevant|skip.
    Non-skip judgments materialize a snippet-shaped dict into the draft; the
    real `examples/` write happens at product materialization (Phase 5).

    `item_id` moved from URL path → form body because RSS/media item ids
    look like `rss:https://example.com/some/deep/path` — the embedded
    slashes broke FastAPI's `{item_id}` path matcher (Starlette rejects
    `%2F` in paths for security), so we hit 404s on every media item.
    """
    _require_flag()
    draft = _get_draft_or_404(slug)
    form = await request.form()
    verdict = (form.get("verdict") or "skip").strip()
    item_id = (form.get("item_id") or "").strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="missing item_id in form body")
    if verdict not in ("relevant", "not_relevant", "skip"):
        raise HTTPException(status_code=400, detail=f"bad verdict {verdict!r}")

    if not draft.calibration:
        draft.calibration = {"judgments": {}}
    judgments = draft.calibration.setdefault("judgments", {})

    # Look the item up in the corpus so we can persist the fields the snippet
    # writer needs (title, body, source_url, created_at).
    from pipeline.minifetch import load_corpus
    lookup = {it["id"]: it for it in load_corpus(slug)}
    item = lookup.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"item {item_id!r} not in corpus")

    entry = {
        "verdict": verdict,
        "item_id": item_id,
        "title": item.get("title") or "",
        "body": item.get("body") or "",
        "source_url": item.get("url") or "",
        "source_display_name": item.get("source_display_name") or item.get("source") or "",
        "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if verdict in _VERDICT_TO_POLARITY:
        entry["polarity"] = _VERDICT_TO_POLARITY[verdict]
    judgments[item_id] = entry
    _wv2.save_draft(_products_dir(), draft)
    return RedirectResponse(url=f"/wizard/{slug}", status_code=303)


# ---------------------------------------------------------------------------
# Screen 4 (Review) — cost estimate + LLM chooser + create-and-run
# ---------------------------------------------------------------------------


# Rough per-item token averages used by the cost preview. Deliberately
# conservative — this is "estimate", not "invoice".
_PER_ITEM_PROMPT_TOKENS = 900
_PER_ITEM_COMPLETION_TOKENS = 200
_EXTRAPOLATION_FACTOR = 20  # minifetch is 1 page × 30 hits; full run is ~ this many


def _provider_presets() -> dict:
    from pipeline.wizard import PROVIDER_PRESETS
    return PROVIDER_PRESETS


def _available_llm_options() -> list[dict]:
    """Return the pre-configured LLM connections a user can pick from on the
    review screen. Each option has enough info to pick without typing an
    endpoint, model, or key. Shape:
      {choice: hosted|ollama|skip, provider: <id>, display, model,
       endpoint, source: ".env"|"local"|"none",
       badge: short human tag, is_default: bool}

    The list is filtered — options requiring a missing key aren't shown, so
    the user only picks from things that will actually work. The admin's
    default LLM provider (set on /connections) is moved to the top and
    tagged so it's the natural first pick.

    NOTE: The assistant LLM is deliberately NOT surfaced here. That's the
    wizard's own connection for drafting/taxonomy — mixing it with the
    product's runtime relevance/classify LLM leaks the wizard's tokens
    into the product's monthly budget and confuses the mental model. The
    /connections tab is the source of truth for runtime LLM keys.
    """
    from pipeline import admin_defaults as _defaults
    from pipeline.wizard import PROVIDER_PRESETS

    default_provider = _defaults.default_llm_provider()
    options: list[dict] = []
    # Any provider whose connections env var is already set in .env / os.environ.
    for pid, preset in PROVIDER_PRESETS.items():
        env_name = preset.get("api_key_env")
        if not env_name or not _env_has_key(env_name):
            continue
        is_default = pid == default_provider
        options.append({
            "choice": "hosted",
            "provider": pid,
            "display": f"{preset.get('display', pid)} ({preset.get('model', '')})",
            "model": preset.get("model", ""),
            "endpoint": preset.get("endpoint", ""),
            "source": ".env",
            "badge": ("default" if is_default else f"key in {env_name}"),
            "is_default": is_default,
        })
    # Ollama is always available in principle — user's responsibility to
    # start `ollama serve`. Health check on save catches unreachable.
    options.append({
        "choice": "ollama",
        "provider": "ollama",
        "display": f"Local (Ollama · {DEFAULT_OLLAMA_MODEL})",
        "model": DEFAULT_OLLAMA_MODEL,
        "endpoint": DEFAULT_OLLAMA_ENDPOINT,
        "source": "local",
        "badge": ("default" if default_provider == "ollama"
                   else "requires ollama running"),
        "is_default": default_provider == "ollama",
    })
    # Skip is a fetch-only run. Always available.
    options.append({
        "choice": "skip",
        "provider": "",
        "display": "Skip — fetch only (no LLM calls)",
        "model": "",
        "endpoint": "",
        "source": "none",
        "badge": "partial run",
        "is_default": False,
    })
    # Move the default to the top so the wizard pre-selects it.
    options.sort(key=lambda o: (0 if o.get("is_default") else 1))
    return options


# Constants imported lazily to avoid circular imports.
try:
    from pipeline.wizard import DEFAULT_OLLAMA_ENDPOINT, DEFAULT_OLLAMA_MODEL
except Exception:
    DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434/v1"
    DEFAULT_OLLAMA_MODEL = "llama3.1:8b"


def _cost_estimate(draft: _wv2.WizardV2Draft) -> dict:
    """Crude per-run cost preview. Returns
    {items, prompt_tokens, completion_tokens, cost_usd (optional)}.
    Uses the chosen llm_model if any; else empty pricing."""
    from pipeline.token_usage import estimate_cost_usd
    corpus_size = 0
    try:
        corpus_size = len(_minifetch.load_corpus(draft.slug))
    except Exception:
        pass
    est_items = max(corpus_size, 1) * _EXTRAPOLATION_FACTOR
    p_tokens = est_items * _PER_ITEM_PROMPT_TOKENS
    c_tokens = est_items * _PER_ITEM_COMPLETION_TOKENS
    # cost_unknown_reason: `""` when we have a dollar figure, `no_llm` when
    # the user hasn't picked an LLM yet (so nothing to price against),
    # `no_pricing` when the picked model isn't in config/model_pricing.yaml.
    # The template renders a helpful nudge in each case.
    cost = None
    reason = ""
    if not draft.llm_model or draft.llm_choice == "skip":
        reason = "no_llm"
    else:
        cost = estimate_cost_usd(draft.llm_model, p_tokens, c_tokens, 0)
        if cost is None:
            reason = "no_pricing"
    return {
        "n_items": est_items,
        "prompt_tokens": p_tokens,
        "completion_tokens": c_tokens,
        "cost_usd": cost,
        "cost_unknown_reason": reason,
    }


@router.post("/wizard/{slug}/llm")
async def wizard_choose_llm(slug: str, request: Request):
    """Save the LLM chooser selection.

    The Review screen shows a curated list of *already-configured* LLM
    options — assistant LLM, providers whose key is in .env, Ollama, skip.
    The user just picks one; no endpoint/model/key entry required. The
    form value `option_index` names which entry from `_available_llm_options()`
    they selected; we pull endpoint + model + choice from that.
    """
    _require_flag()
    draft = _get_draft_or_404(slug)
    form = await request.form()

    options = _available_llm_options()
    try:
        idx = int(form.get("option_index") or "-1")
    except ValueError:
        idx = -1
    if not (0 <= idx < len(options)):
        return RedirectResponse(url=f"/wizard/{slug}?error=pick+an+llm+option",
                                status_code=303)

    picked = options[idx]
    draft.llm_choice = picked["choice"]
    draft.llm_provider = picked["provider"]
    draft.llm_endpoint = picked["endpoint"]
    draft.llm_model = picked["model"]

    # Optional inline health check — best-effort. Failure surfaces on the
    # review page but does not block the flow.
    if picked["choice"] != "skip":
        ok, msg = _probe_llm(draft)
        draft.llm_health_ok = ok
        draft.llm_health_message = msg
    else:
        draft.llm_health_ok = None
        draft.llm_health_message = ""

    _wv2.save_draft(_products_dir(), draft)
    return RedirectResponse(url=f"/wizard/{slug}", status_code=303)


def _persist_env(name: str, value: str) -> None:
    """Write to .env AND sync os.environ (see pipeline/env_writer.py)."""
    from pipeline import env_writer
    try:
        env_writer.set_var(_env_path(), name, value)
    except Exception:
        pass


def _probe_llm(draft: _wv2.WizardV2Draft) -> tuple[bool, str]:
    """Cheap health check for the per-product LLM routing selected on the
    Review screen. Uses the same chat.completions probe as the assistant-
    LLM setup — `models.list()` returns 401 on Anthropic's OpenAI-compat
    layer even with a valid key, so it can't be used as a probe.
    Endpoint gets trailing-slash normalization to satisfy Anthropic's
    URL join requirement.
    """
    routing = _wv2._build_llm_routing(draft)
    if routing is None:
        return False, "no routing built"
    try:
        import os
        from openai import OpenAI
        from pipeline.llm import _resolve_api_key
        cfg = routing["relevance"]
        api_key = _resolve_api_key(cfg, dict(os.environ))
        # Also merge in .env so a freshly-saved key is visible without a
        # server restart (dotenv writes the file but doesn't push into
        # os.environ in the running process).
        if not api_key or api_key == "not-needed-for-local":
            try:
                from dotenv import dotenv_values
                env_file = Path(__file__).resolve().parent.parent / ".env"
                if env_file.exists():
                    merged = {**dict(os.environ),
                              **{k: v for k, v in (dotenv_values(env_file) or {}).items() if v}}
                    api_key = _resolve_api_key(cfg, merged)
            except Exception:
                pass
        base_url = _normalize_endpoint(cfg["endpoint"])
        client = OpenAI(base_url=base_url, api_key=api_key or "not-needed-for-local",
                        timeout=15)
        # Minimal chat completion — same 1-token probe LLMClient uses at
        # pipeline runtime, so a green here means runs will proceed.
        client.chat.completions.create(
            model=cfg["model"],
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        return True, "endpoint reachable"
    except Exception as e:
        msg = str(e)
        if "401" in msg or "invalid" in msg.lower() or "authentication" in msg.lower():
            return False, f"401 — API key rejected. Check /connections/{draft.llm_provider}."
        if "404" in msg or "not found" in msg.lower():
            return False, f"404 — endpoint or model not recognized ({cfg.get('model')})"
        return False, msg[:200]


@router.post("/wizard/{slug}/create")
async def wizard_create(slug: str, request: Request):
    """Materialize the draft into a real product on disk.

    The review form is unified: the LLM radio + Create button share one
    form so `option_index` comes with the create POST. If the user hasn't
    picked one, we redirect back with an error rather than silently
    materializing a broken product that ships with the scaffold's
    Foundry-Local default endpoint (which fails health check on every
    subsequent run).

    Does NOT trigger a pipeline run — historically we auto-ran here, but
    users expected "Create" to mean create. The redirect lands on Runs so
    Trigger run is one click away.
    """
    _require_flag()
    draft = _get_draft_or_404(slug)

    # Apply LLM chooser selection from the merged form.
    form = await request.form()
    options = _available_llm_options()
    idx_raw = form.get("option_index")
    if idx_raw is None:
        return RedirectResponse(
            url=(f"/wizard/{slug}?error=pick+an+LLM+option+in+the+LLM+section"
                 f"+above+before+clicking+Create"),
            status_code=303,
        )
    try:
        idx = int(idx_raw)
    except (TypeError, ValueError):
        idx = -1
    if not (0 <= idx < len(options)):
        return RedirectResponse(
            url=f"/wizard/{slug}?error=invalid+LLM+selection",
            status_code=303,
        )
    picked = options[idx]
    draft.llm_choice = picked["choice"]
    draft.llm_provider = picked["provider"]
    draft.llm_endpoint = picked["endpoint"]
    draft.llm_model = picked["model"]

    # Save the picked LLM into the draft first so materialize sees it.
    # If the user chose "skip", we deliberately continue — the pipeline
    # will simply run in fetch-only mode without complaining about a
    # broken health check (see the "skip" arm in _build_llm_routing).
    _wv2.save_draft(_products_dir(), draft)

    try:
        product_dir = _wv2.materialize(draft, _products_dir())
    except Exception as e:
        return RedirectResponse(
            url=f"/wizard/{slug}?error=create+failed:+{str(e)[:120]}",
            status_code=303,
        )
    # Clean up the wizard's temp corpus now that the product owns the snippets.
    _minifetch.discard_corpus(slug)
    return RedirectResponse(
        url=f"/products/{slug}/runs?notice=product+created+-+click+Trigger+run+when+ready",
        status_code=303,
    )


def _trigger_first_run(product_id: str) -> None:
    """Kick off the same fire-and-forget pipeline the /products/*/runs route
    uses. Runs in a subprocess so it survives our request cycle."""
    import subprocess
    import sys
    import uuid
    from datetime import datetime, timezone

    marker_id = (
        f"wizard-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-"
        f"{uuid.uuid4().hex[:6]}"
    )
    try:
        from pipeline.config import app_config, resolve_path
        logs_dir = resolve_path(app_config()["paths"]["run_logs_root"]) / product_id
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / f"{marker_id}.running").write_text(
            f"launched by wizard v2 at {datetime.now(timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
        cmd = [
            sys.executable, "-m", "pipeline.run",
            "--product", product_id,
            "--run-id", marker_id,
        ]
        subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=(logs_dir / f"{marker_id}.out").open("w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) if sys.platform == "win32" else 0,
        )
    except Exception:
        # If we can't launch the run, the product still exists — user can
        # trigger a run manually from the runs page. Silently swallow.
        pass
