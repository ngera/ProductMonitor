"""Shared helpers for the web UI routers (ADR-0026).

Everything that more than one router uses, plus the names tests patch
(PRODUCTS_DIR, ENV_FILE_PATH, app_config, ...). Routers read these as
`core.<name>` at call time. Moved verbatim out of webui/app.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
import json as _json
import os
import re
import shutil
import subprocess
import sys

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
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
from datetime import date, datetime, timezone
import uuid
from fastapi import Body

from dotenv import dotenv_values, set_key, unset_key

from pipeline.product import (
    PRODUCTS_DIR,
    available_products,
    clear_cache,
    load_product,
    save_product_facts,
    save_product_meta,
    scaffold_product,
)

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _theme_from_request(request: Request) -> Optional[str]:
    """Read pm_theme cookie (ADR-0032). Absent → None (prefers-color-scheme)."""
    t = request.cookies.get("pm_theme")
    return t if t in ("dark", "light") else None


_orig_template_response = templates.TemplateResponse


def _template_response_with_theme(name, context, **kwargs):
    """Inject theme into every TemplateResponse so base.html can set data-theme."""
    ctx = dict(context or {})
    req = ctx.get("request")
    if req is not None and "theme" not in ctx:
        ctx["theme"] = _theme_from_request(req)
    return _orig_template_response(name, ctx, **kwargs)


templates.TemplateResponse = _template_response_with_theme  # type: ignore[method-assign]

# --- Scheduler daemon --------------------------------------------------------
# One background thread across the whole webui process fires due pipeline runs
# per product. Idempotency: module-level flag prevents accidental double-start
# on reloads. See webui/scheduler_runtime.py for the tick loop.
_scheduler_started = False

_FEATURES_YAML = Path(__file__).resolve().parent.parent / "config" / "features.yaml"

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
             "default": "product-monitor:0.1 (by /u/yourname)",
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
    # --- Cloud providers (OpenAI-compat, API key required) -------------------
    "azure_openai": {
        "category": "llm",
        "display": "Azure OpenAI",
        "url": "https://portal.azure.com",
        # No single canonical endpoint — each Azure OpenAI resource has its own.
        # Leave blank in the recommended UI; operator pastes theirs in llm_routing.yaml.
        "api_endpoint": "",
        "endpoint_hints": ["openai.azure.com"],
        "help": (
            "Azure-hosted OpenAI. Create an OpenAI resource + model deployment "
            "in the Azure portal; endpoint is per-resource. Format: "
            "https://<your-resource>.openai.azure.com/openai/deployments/<deployment>. "
            "Set both the API key and the endpoint below."
        ),
        "fields": [
            {"env": "AZURE_OPENAI_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Key 1 or Key 2 from your Azure OpenAI resource's Keys and Endpoint page."},
            {"env": "AZURE_OPENAI_ENDPOINT", "label": "Endpoint URL", "type": "text", "default": "",
             "help": "Full deployment URL. Copy from the Azure portal for your deployment."},
        ],
        "recommended_models": [
            {"id": "gpt-4o",      "purpose": "classify — balanced default"},
            {"id": "gpt-4o-mini", "purpose": "relevance — cheap, fast"},
        ],
    },
    "openrouter": {
        "category": "llm",
        "display": "OpenRouter",
        "url": "https://openrouter.ai",
        "api_endpoint": "https://openrouter.ai/api/v1",
        "endpoint_hints": ["openrouter.ai"],
        "help": (
            "Proxy for many providers via one key. Useful when you want to "
            "route different roles to different underlying models without "
            "juggling per-provider keys. Free tier available for many models."
        ),
        "fields": [
            {"env": "OPENROUTER_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Starts with 'sk-or-…'. Get it at https://openrouter.ai/keys."},
        ],
        "recommended_models": [
            {"id": "anthropic/claude-sonnet-4",           "purpose": "classify via Anthropic"},
            {"id": "openai/gpt-4o-mini",                  "purpose": "relevance via OpenAI"},
            {"id": "meta-llama/llama-3.3-70b-instruct",   "purpose": "classify — open model"},
        ],
    },
    "groq": {
        "category": "llm",
        "display": "Groq",
        "url": "https://console.groq.com",
        "api_endpoint": "https://api.groq.com/openai/v1",
        "endpoint_hints": ["groq.com"],
        "help": (
            "LPU-hosted open-weight models. Very fast token throughput at "
            "low cost. Endpoint: https://api.groq.com/openai/v1"
        ),
        "fields": [
            {"env": "GROQ_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Starts with 'gsk_…'. Get it at https://console.groq.com/keys."},
        ],
        "recommended_models": [
            {"id": "llama-3.3-70b-versatile",   "purpose": "classify — balanced default"},
            {"id": "llama-3.1-8b-instant",      "purpose": "relevance — cheap, fast"},
            {"id": "mixtral-8x7b-32768",        "purpose": "classify — long-context"},
        ],
    },
    "together": {
        "category": "llm",
        "display": "Together.ai",
        "url": "https://api.together.ai",
        "api_endpoint": "https://api.together.xyz/v1",
        "endpoint_hints": ["together.xyz", "together.ai"],
        "help": (
            "Hosted open-weight models with a long catalog. Endpoint: "
            "https://api.together.xyz/v1"
        ),
        "fields": [
            {"env": "TOGETHER_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Get it at https://api.together.ai/settings/api-keys."},
        ],
        "recommended_models": [
            {"id": "meta-llama/Llama-3.3-70B-Instruct-Turbo",     "purpose": "classify"},
            {"id": "meta-llama/Llama-3.2-3B-Instruct-Turbo",      "purpose": "relevance"},
            {"id": "Qwen/Qwen2.5-72B-Instruct-Turbo",             "purpose": "classify — strong instructions"},
        ],
    },
    "fireworks": {
        "category": "llm",
        "display": "Fireworks AI",
        "url": "https://fireworks.ai",
        "api_endpoint": "https://api.fireworks.ai/inference/v1",
        "endpoint_hints": ["fireworks.ai"],
        "help": (
            "Hosted open models + custom fine-tunes. Endpoint: "
            "https://api.fireworks.ai/inference/v1"
        ),
        "fields": [
            {"env": "FIREWORKS_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Get it at https://fireworks.ai/api-keys."},
        ],
        "recommended_models": [
            {"id": "accounts/fireworks/models/llama-v3p3-70b-instruct",   "purpose": "classify"},
            {"id": "accounts/fireworks/models/llama-v3p1-8b-instruct",    "purpose": "relevance"},
        ],
    },
    "deepinfra": {
        "category": "llm",
        "display": "DeepInfra",
        "url": "https://deepinfra.com",
        "api_endpoint": "https://api.deepinfra.com/v1/openai",
        "endpoint_hints": ["deepinfra.com"],
        "help": (
            "Hosted open models priced per token. Endpoint: "
            "https://api.deepinfra.com/v1/openai"
        ),
        "fields": [
            {"env": "DEEPINFRA_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Get it at https://deepinfra.com/dash/api_keys."},
        ],
        "recommended_models": [
            {"id": "meta-llama/Meta-Llama-3.1-70B-Instruct",   "purpose": "classify"},
            {"id": "meta-llama/Meta-Llama-3.1-8B-Instruct",    "purpose": "relevance"},
        ],
    },
    "perplexity": {
        "category": "llm",
        "display": "Perplexity",
        "url": "https://www.perplexity.ai",
        "api_endpoint": "https://api.perplexity.ai",
        "endpoint_hints": ["perplexity.ai"],
        "help": (
            "Sonar models with built-in web search. Useful when the pipeline "
            "benefits from up-to-date grounded output. Endpoint: "
            "https://api.perplexity.ai"
        ),
        "fields": [
            {"env": "PERPLEXITY_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Starts with 'pplx-…'. Get it at https://www.perplexity.ai/settings/api."},
        ],
        "recommended_models": [
            {"id": "sonar-small-online",   "purpose": "relevance — web-grounded"},
            {"id": "sonar-large-online",   "purpose": "classify — web-grounded"},
        ],
    },
    "mistral": {
        "category": "llm",
        "display": "Mistral La Plateforme",
        "url": "https://console.mistral.ai",
        "api_endpoint": "https://api.mistral.ai/v1",
        "endpoint_hints": ["mistral.ai"],
        "help": (
            "Mistral's own hosted API. Endpoint: https://api.mistral.ai/v1"
        ),
        "fields": [
            {"env": "MISTRAL_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Get it at https://console.mistral.ai/api-keys."},
        ],
        "recommended_models": [
            {"id": "mistral-large-latest",   "purpose": "classify — flagship"},
            {"id": "mistral-small-latest",   "purpose": "relevance — cheap, fast"},
            {"id": "ministral-8b-latest",    "purpose": "relevance — smallest"},
        ],
    },
    "cohere": {
        "category": "llm",
        "display": "Cohere",
        "url": "https://cohere.com",
        "api_endpoint": "https://api.cohere.com/compatibility/v1",
        "endpoint_hints": ["cohere.com", "cohere.ai"],
        "help": (
            "Cohere Command R / R+ via the OpenAI-compat endpoint. Endpoint: "
            "https://api.cohere.com/compatibility/v1"
        ),
        "fields": [
            {"env": "COHERE_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Get it at https://dashboard.cohere.com/api-keys."},
        ],
        "recommended_models": [
            {"id": "command-r-plus",     "purpose": "classify — flagship"},
            {"id": "command-r",          "purpose": "relevance — balanced"},
        ],
    },
    # --- Local / self-hosted runtimes (no auth by default) -------------------
    "vllm": {
        "category": "llm",
        "display": "vLLM (self-hosted)",
        "url": "https://docs.vllm.ai",
        "api_endpoint": "http://localhost:8000/v1",
        "endpoint_hints": ["8000", "vllm"],
        "help": (
            "Self-hosted vLLM server. Start with `vllm serve <model>`; "
            "default port 8000. No auth by default — if you started vLLM with "
            "`--api-key`, set VLLM_API_KEY in .env and reference it via "
            "`api_key_env: VLLM_API_KEY` in llm_routing.yaml. Endpoint: "
            "http://localhost:8000/v1"
        ),
        "fields": [],  # not-needed status; local deployment
        "recommended_models": [
            {"id": "meta-llama/Llama-3.1-8B-Instruct",   "purpose": "any role — start small"},
            {"id": "Qwen/Qwen2.5-32B-Instruct",          "purpose": "classify — larger"},
        ],
    },
    "lmstudio": {
        "category": "llm",
        "display": "LM Studio (local desktop)",
        "url": "https://lmstudio.ai",
        "api_endpoint": "http://localhost:1234/v1",
        "endpoint_hints": ["1234", "lmstudio", "lm-studio"],
        "help": (
            "Desktop app that runs local models with a one-click server. "
            "Load a model in LM Studio, click 'Start Server', point the "
            "pipeline at http://localhost:1234/v1. No auth."
        ),
        "fields": [],  # not-needed status
        "recommended_models": [
            {"id": "any loaded model", "purpose": "LM Studio serves whatever's currently loaded — model id shown in the app's Server tab"},
        ],
    },
    "tabbyapi": {
        "category": "llm",
        "display": "TabbyAPI (llama.cpp-based)",
        "url": "https://github.com/theroyallab/tabbyAPI",
        "api_endpoint": "http://localhost:5000/v1",
        "endpoint_hints": ["5000", "tabby"],
        "help": (
            "OpenAI-compat frontend for llama.cpp / exllamav2. Start "
            "TabbyAPI, load your model via its config, point the pipeline "
            "at http://localhost:5000/v1. If you enabled auth in TabbyAPI's "
            "config.yml, set TABBY_API_KEY in .env and reference it via "
            "`api_key_env: TABBY_API_KEY` in llm_routing.yaml."
        ),
        "fields": [],  # not-needed status by default
        "recommended_models": [
            {"id": "any loaded model", "purpose": "TabbyAPI serves whatever's in its model dir — see the app for exact id"},
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

_SCRAPECREATORS_CONNECTION_META = {
    "scrapecreators": {
        "category": "source",
        "display": "ScrapeCreators",
        "url": "https://scrapecreators.com/",
        "help": (
            "Shared API key for the Reddit / X / TikTok ScrapeCreators plugins. "
            "One key covers all three; the individual plugins read from it via "
            "the shared client. Pause here to disable all three at once."
        ),
        "fields": [
            {
                "env": "SCRAPECREATORS_API_KEY",
                "label": "ScrapeCreators API Key",
                "type": "secret",
                "default": "",
                "help": "One key covers Reddit + X + TikTok SC plugins. Get it at scrapecreators.com.",
            },
        ],
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
    conn_meta.update(_SCRAPECREATORS_CONNECTION_META)
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
    try:
        from pipeline.ollama_lifecycle import default_openai_compat_endpoint
        ollama_ep = default_openai_compat_endpoint()
    except Exception:
        ollama_ep = "http://localhost:11434/v1"
    out = []
    for type_id, meta in CONNECTION_META.items():
        if meta.get("category") != "llm":
            continue
        api_endpoint = meta.get("api_endpoint") or ""
        # Docker-aware default so the routing form probes the host's Ollama
        # the same way /wizard/llm does — not container-localhost.
        if type_id == "ollama":
            api_endpoint = ollama_ep
        out.append({
            "type": type_id,
            "display": meta.get("display") or type_id,
            "api_endpoint": api_endpoint,
            "endpoint_hints": meta.get("endpoint_hints") or [],
            "models": [
                {"id": m["id"], "purpose": m.get("purpose", "")}
                for m in (meta.get("recommended_models") or [])
            ],
        })
    return out


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
    "reddit_rss":          "subreddit",       # bare name; UI renders as r/name
    "hn":                  "search_queries",  # list; take first for label
    "github_issues":       "repos",           # list
    "microsoft_community": "feed_url",
    "stackex":             "site",
    "discourse":           "host",
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
    # reddit_rss legacy compat: pre-migration streams may carry `feed_url`
    # without `subreddit`. Extract a display name from the URL if we can.
    if stream_type == "reddit_rss" and (v is None or v == ""):
        legacy = stream.get("feed_url") or ""
        if legacy:
            from sources.reddit_rss import normalize_subreddit
            v = normalize_subreddit(legacy) or legacy
    if isinstance(v, list):
        if not v:
            return "—"
        first = v[0]
        if len(v) == 1:
            return str(first)
        return f"{first} +{len(v) - 1} more"
    if v is None or v == "":
        return stream.get("name") or "—"
    if stream_type in ("reddit", "reddit_rss"):
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


def _product_dir_for(product_id: str) -> Path:
    d = PRODUCTS_DIR / product_id
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"product '{product_id}' not found")
    return d


# --- Snippets (UI 3) --------------------------------------------------------


def _product_or_404(product_id: str):
    try:
        p = load_product(product_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # Set as current so downstream storage.warehouse() / current_product()
    # resolve paths to this product's warehouse.duckdb rather than whichever
    # product a prior request set. Without this, per-request calls into
    # pipeline modules that use the "current product" indirection (e.g.
    # token_usage.per_run_totals, engagement.distribution_by_source) open
    # the wrong DB.
    set_current_product(p)
    return p


# --- Runs + reports (UI 4) --------------------------------------------------
#
# Run trigger spawns `python -m pipeline.run --product <id>` as a subprocess.
# Status is read from data/<product>/run_logs/<run_id>.json (written by the
# pipeline at the end) and from a sidecar .running marker file we drop before
# starting the subprocess. Stdout/stderr go to .out so the user can see what
# happened on failure.


# These stay as module-level shims for the (currently) many inline routes
# that reference them. New code should import from webui.services.runs
# directly (ADR-0026). When a route referencing these is migrated out to
# its own router, update it to call the service and drop the shim once
# nothing else references it.
def _product_data_root(product_id: str) -> Path:
    from webui.services.runs import product_data_root
    return product_data_root(product_id)


def _run_logs_dir(product_id: str) -> Path:
    from webui.services.runs import run_logs_dir
    return run_logs_dir(product_id)


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


# Stage order matches pipeline.run.PIPELINE_STAGES (ADR-0033). Kept in sync
# manually — small, rarely changes. Used by the flow-diagram parser and
# resume-stage inference below.
_PIPELINE_STAGES: tuple[str, ...] = (
    "fetch", "normalize", "filter",
    "relevance", "classify", "score", "group",
    "persistent_issue", "aggregate", "eval", "render", "digest",
)


def _spawn_pipeline_run(
    product_id: str,
    *,
    skip_fetch: bool = False,
    skip_llm: bool = False,
    source_ids: Optional[list[str]] = None,
    keep_feed_urls: Optional[list[str]] = None,
    time_mode: Optional[str] = None,
    from_stage: Optional[str] = None,
    week_id: Optional[str] = None,
    resumed_from: Optional[str] = None,
) -> str:
    """Fire-and-forget a UI pipeline subprocess; return the marker run_id."""
    import uuid
    from datetime import datetime, timezone

    marker_id = f"ui-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    logs_dir = _run_logs_dir(product_id)
    logs_dir.mkdir(parents=True, exist_ok=True)
    marker = logs_dir / f"{marker_id}.running"
    marker.write_text(
        f"started by webui at {datetime.now(timezone.utc).isoformat()}\n",
        encoding="utf-8",
    )
    # Pre-seed prior stage snapshots so the detail page has source counts
    # before the subprocess finishes init_run (ADR-0033).
    if resumed_from and from_stage and from_stage in _PIPELINE_STAGES:
        prior_stages = list(
            _PIPELINE_STAGES[: _PIPELINE_STAGES.index(from_stage)]
        )
        try:
            from pipeline import stage_capture as _sc
            _sc.inherit_prior_stages(
                product_id, marker_id, resumed_from, prior_stages,
            )
        except Exception:
            pass
        (logs_dir / f"{marker_id}.resume.json").write_text(
            _json.dumps({
                "prior_run_id": resumed_from,
                "from_stage": from_stage,
                "week_id": week_id,
            }, indent=2),
            encoding="utf-8",
        )
    out_path = logs_dir / f"{marker_id}.out"
    cmd = [
        _project_python(), "-m", "pipeline.run",
        "--product", product_id,
        "--run-id", marker_id,
    ]
    if skip_fetch:
        cmd.append("--skip-fetch")
    if skip_llm:
        cmd.append("--skip-llm")
    if from_stage:
        cmd.extend(["--from-stage", from_stage])
    if resumed_from:
        cmd.extend(["--resumed-from", resumed_from])
    if week_id:
        cmd.extend(["--week", week_id])
    if source_ids:
        cmd.extend(["--source-ids", ",".join(source_ids)])
    if keep_feed_urls:
        cmd.extend(["--keep-feed-urls", ",".join(keep_feed_urls)])
    if time_mode and time_mode != "saved":
        cmd.extend(["--time-mode", time_mode])

    try:
        env = os.environ.copy()
        # Windows consoles default to cp1252; force UTF-8 so emoji in LLM
        # error snippets can't abort the detached pipeline subprocess.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=open(out_path, "w", encoding="utf-8", errors="replace"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) if sys.platform == "win32" else 0,
        )
    except Exception as e:
        marker.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"failed to spawn pipeline: {e}")

    (logs_dir / f"{marker_id}.pid").write_text(str(proc.pid), encoding="utf-8")
    return marker_id


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
