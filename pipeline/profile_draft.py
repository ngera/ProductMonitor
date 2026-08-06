"""Profile drafting service (wizard redesign Phase 2).

Turns Screen 1 input (name, url_or_description, goals) into a full drafted
profile the wizard shows on Screen 2. One assistant-LLM call, structured
output, ADR-0007-style contract with retry.

The user never sees a blank field: description / aliases / confusables /
scope bullets / competitors / suggested sources all come pre-populated from
the drafting call. The user edits chips/cards and confirms.

Key design points:
- URL fetch is best-effort. Fails → `page_fetch_failed=True` in the result and
  the caller surfaces a "we couldn't read the page" banner. The draft still
  runs against name + goals.
- `suggested_sources` are validated against the source registry: unknown
  plugin_ids are dropped; `requires_key` is computed from the plugin's
  manifest connection_fields plus current env state.
- Token attribution: every call runs under a `set_context(...)` block with
  stage="assistant_wizard_draft" so the assistant-LLM budget check
  (which does `WHERE stage LIKE 'assistant_%'`) picks it up.
- Regeneration caps live on the wizard draft file (per-section counters,
  cap 3). This module is stateless — the wizard router enforces the cap
  before calling us.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import structlog
from pydantic import BaseModel, Field, model_validator

from pipeline.product import VALID_GOALS
from pipeline.token_usage import TokenContext, set_context

log = structlog.get_logger()


# Fetching config
URL_FETCH_TIMEOUT_SECONDS = 10.0
MAX_URL_FETCH_CHARS = 8000
_USER_AGENT = "product-monitor-wizard/1.0 (+profile-draft)"

# LLM contract tuning — small caps to protect the assistant-LLM budget.
MAX_ALIASES = 8
MAX_CONFUSABLES = 5
MAX_COMPETITORS = 6
MAX_SCOPE_BULLETS = 5
MAX_SOURCES_SUGGESTED = 8


# ---------------------------------------------------------------------------
# Structured output models (ADR-0007)
# ---------------------------------------------------------------------------


class SuggestedSource(BaseModel):
    """One source suggestion from the drafting LLM.

    `stream_config` is a dict matching the plugin's `stream_fields` (validated
    loosely — we don't reject unknown keys, since the wizard can prune them,
    but see `_validate_and_annotate_sources()` for the drop-unknown-plugin
    step).

    The `coerce_string` validator handles a real LLM regression pattern:
    when the structured-output path fails and the LLM falls back to
    plain-JSON emission, it sometimes shortens each item to just the
    plugin_id string (e.g. `["hn", "reddit"]`). Rather than fail 5
    validation errors and abort the whole draft, we upgrade a bare string
    to `{"plugin_id": <str>}`.
    """

    plugin_id: str = Field(description="Registered source plugin id (e.g., 'hn', 'rss').")
    stream_config: dict = Field(
        default_factory=dict,
        description="Stream config matching the plugin's stream_fields.",
    )
    rationale: str = Field(default="", description="One-line justification shown in UI.")
    # Populated post-hoc from the registry — the LLM doesn't have to know.
    requires_key: bool = False

    @model_validator(mode="before")
    @classmethod
    def _coerce_string(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"plugin_id": data}
        return data


class ProfileDraft(BaseModel):
    """Everything the wizard's Screen 2 needs to render, pre-populated."""

    description: str = Field(description="Two-to-four sentence description, <=120 words.")
    aliases: list[str] = Field(default_factory=list, description="Names the product is known by.")
    not_to_be_confused_with: list[str] = Field(
        default_factory=list, description="Similarly-named products to exclude.",
    )
    competitors: list[str] = Field(default_factory=list)
    scope_in: list[str] = Field(
        default_factory=list,
        description="2-4 short bullets describing what's in scope.",
    )
    scope_out: list[str] = Field(
        default_factory=list,
        description="1-3 short bullets describing what's out of scope.",
    )
    suggested_sources: list[SuggestedSource] = Field(
        default_factory=list,
        description="Ordered by likely usefulness. Keyless sources first.",
    )


@dataclass
class DraftResult:
    """What `draft_profile()` returns to the caller.

    `profile` is None when the assistant LLM isn't configured OR when every
    call attempt failed. `page_fetch_failed` is set only when a URL was
    supplied and its fetch failed; unset on description-only input.
    """

    profile: Optional[ProfileDraft]
    page_fetch_failed: bool = False
    fetched_chars: int = 0
    error_message: str = ""


# ---------------------------------------------------------------------------
# URL fetching
# ---------------------------------------------------------------------------


def _fetch_url_text(url: str) -> tuple[str, bool]:
    """Best-effort HTML → text fetch. Returns (text, fetch_failed).

    On success returns the extracted text (capped at MAX_URL_FETCH_CHARS)
    and False. On any error (timeout, non-2xx, HTML parse failure) returns
    ("", True) — callers degrade to name+goals-only drafting.
    """
    if not url or not url.strip():
        return "", False
    try:
        import httpx
        from bs4 import BeautifulSoup
    except Exception as e:
        log.warning("profile_draft.url_fetch_deps_missing", error=str(e))
        return "", True
    try:
        with httpx.Client(
            timeout=URL_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        ) as client:
            resp = client.get(url.strip())
            resp.raise_for_status()
            html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        # Drop obvious noise before extracting text.
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = soup.get_text(separator="\n")
        # Collapse consecutive blank lines.
        lines = [ln.strip() for ln in text.splitlines()]
        cleaned = "\n".join(ln for ln in lines if ln)
        return cleaned[:MAX_URL_FETCH_CHARS], False
    except Exception as e:
        log.warning("profile_draft.url_fetch_failed", url=url, error=str(e))
        return "", True


# ---------------------------------------------------------------------------
# Source registry validation
# ---------------------------------------------------------------------------


def _read_env_snapshot() -> dict[str, str]:
    """Return current os.environ merged with .env (dotenv precedence: env wins)."""
    import os
    snap = dict(os.environ)
    try:
        from dotenv import dotenv_values
        from pathlib import Path
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if env_path.exists():
            for k, v in (dotenv_values(env_path) or {}).items():
                snap.setdefault(k, v or "")
    except Exception:
        pass
    return snap


def _validate_and_annotate_sources(
    suggested: list[SuggestedSource],
) -> list[SuggestedSource]:
    """Drop suggestions with unknown plugin_ids; compute `requires_key`.

    A source `requires_key` when its manifest has any `required` or `secret`
    connection field that isn't currently set in the environment. This mirrors
    `webui.source_health.compute_readiness` so the wizard and the run-time
    surface agree.
    """
    try:
        from sources.registry import get_registry
        reg = get_registry()
    except Exception as e:
        log.warning("profile_draft.registry_load_failed", error=str(e))
        return []
    env = _read_env_snapshot()
    out: list[SuggestedSource] = []
    for s in suggested:
        plugin = reg.get(s.plugin_id)
        if plugin is None:
            log.info("profile_draft.dropped_unknown_plugin", plugin_id=s.plugin_id)
            continue
        manifest = plugin.manifest
        required_env = [
            f for f in manifest.connection_fields
            if getattr(f, "required", False) or getattr(f, "type", "") == "secret"
        ]
        missing = [f.name for f in required_env if not env.get(f.name, "").strip()]
        s.requires_key = bool(missing)
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# LLM contract wiring
# ---------------------------------------------------------------------------


# The system prompt is now editable at Admin > Prompts. The default lives
# in `pipeline/prompt_templates.py` alongside the other master templates.
# Callers should read via `prompt_templates.get("assistant_profile_draft")`
# so an admin override on disk takes effect immediately.
def _draft_system_prompt() -> str:
    from pipeline import prompt_templates
    return prompt_templates.get("assistant_profile_draft")


# Back-compat: some tests import DRAFT_SYSTEM_PROMPT directly. Keep it as
# a module attribute that evaluates lazily on first access via __getattr__.
def __getattr__(name):
    if name == "DRAFT_SYSTEM_PROMPT":
        return _draft_system_prompt()
    raise AttributeError(name)


ALLOWED_PLUGIN_IDS_HINT = (
    "hn, rss, microsoft_community, apple_appstore, reddit, github_issues, "
    "stackex, youtube_comments, producthunt"
)


def _assistant_contract():
    """Return an assistant-LLM contract, or None if unconfigured. Mirrors
    `pipeline.wizard_llm._assistant_contract`."""
    from pipeline import assistant_llm
    from pipeline.llm_contract import LLMResponseContract
    try:
        client = assistant_llm.client()
    except RuntimeError:
        return None
    contract = LLMResponseContract.__new__(LLMResponseContract)
    contract._client = client
    contract.role = "assistant"
    contract.model = client.model
    contract.endpoint = client.endpoint
    return contract


def _wrap(tag: str, content: str) -> str:
    from pipeline.prompt_safety import wrap_user_content, TagKind
    # Route all user-controlled content through the injection defense.
    kind = TagKind.SCRAPED_CONTENT if tag == "page_content" else TagKind.USER_INPUT
    inner = wrap_user_content(content, kind)
    return f"<{tag}>{inner}</{tag}>"


def _build_user_prompt(
    name: str, url_or_description: str, goals: list[str], page_content: str,
) -> str:
    """Compose the user message with tagged sections."""
    parts = [_wrap("name", name.strip())]
    if goals:
        parts.append(_wrap("goals", ", ".join(goals)))
    parts.append(_wrap("allowed_plugin_ids", ALLOWED_PLUGIN_IDS_HINT))
    if page_content:
        parts.append(_wrap("page_content", page_content))
    else:
        # No URL / URL fetch failed → the user's freetext becomes the source.
        parts.append(_wrap("description", (url_or_description or "").strip()))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def draft_profile(
    name: str,
    url_or_description: str,
    goals: Optional[list[str]] = None,
    *,
    product_id_for_budget: Optional[str] = None,
) -> DraftResult:
    """Produce a drafted profile from Screen 1 input.

    `name` is the product name (required). `url_or_description` is either an
    http(s) URL (we fetch + strip HTML) or freetext. `goals` is the subset of
    VALID_GOALS the user selected (empty is OK).

    `product_id_for_budget` is the slug used for assistant-LLM budget
    attribution (typically the wizard draft slug). Omitting it means the
    token-usage row is written without a product_id, so it doesn't count
    against any product's monthly cap.

    Never raises. Returns `DraftResult(profile=None, error_message=...)` on
    any failure so the wizard can render a "drafting unavailable" banner
    without a 500.
    """
    if not name or not name.strip():
        return DraftResult(profile=None, error_message="product name is required")

    goals = [g for g in (goals or []) if g in VALID_GOALS]
    text_is_url = (url_or_description or "").strip().lower().startswith(("http://", "https://"))
    page_content = ""
    page_fetch_failed = False
    if text_is_url:
        page_content, page_fetch_failed = _fetch_url_text(url_or_description)

    contract = _assistant_contract()
    if contract is None:
        return DraftResult(
            profile=None,
            page_fetch_failed=page_fetch_failed,
            fetched_chars=len(page_content),
            error_message="assistant LLM not configured",
        )

    from pipeline.llm_contract import LLMCallSpec
    user_prompt = _build_user_prompt(name, url_or_description or "", goals, page_content)
    spec = LLMCallSpec(
        system=_draft_system_prompt(),
        user=user_prompt,
        response_model=ProfileDraft,
        cacheable_system=True,
        max_retries=1,
    )
    ctx = TokenContext(
        stage="assistant_wizard_draft",
        product_id=product_id_for_budget or "",
    )
    try:
        with set_context(ctx):
            profile: ProfileDraft = contract.call(spec)  # type: ignore[assignment]
    except Exception as e:
        log.warning("profile_draft.llm_failed", error=str(e))
        return DraftResult(
            profile=None,
            page_fetch_failed=page_fetch_failed,
            fetched_chars=len(page_content),
            error_message=f"drafting failed: {e}",
        )

    # Trim over-long lists per the doc caps (LLM sometimes ignores them).
    profile.aliases = list(dict.fromkeys(profile.aliases or []))[:MAX_ALIASES]
    profile.not_to_be_confused_with = list(dict.fromkeys(
        profile.not_to_be_confused_with or []))[:MAX_CONFUSABLES]
    profile.competitors = list(dict.fromkeys(profile.competitors or []))[:MAX_COMPETITORS]
    profile.scope_in = (profile.scope_in or [])[:MAX_SCOPE_BULLETS]
    profile.scope_out = (profile.scope_out or [])[:MAX_SCOPE_BULLETS]
    profile.suggested_sources = _validate_and_annotate_sources(
        (profile.suggested_sources or [])[:MAX_SOURCES_SUGGESTED],
    )
    return DraftResult(
        profile=profile,
        page_fetch_failed=page_fetch_failed,
        fetched_chars=len(page_content),
    )
