"""Per-stream identifier suggestions for wizard Step 3.

For sources that need a per-stream identifier the wizard can't invent
from the product profile (subreddit for Reddit, feed_url for RSS /
microsoft_community, app_id for Apple App Store, etc.), we call the
assistant LLM with the profile facts + the plugin/field context and get
back 3-8 concrete candidates the user picks from as checkboxes.

Cached on the draft file so we don't re-call on every page load. The
route calls this once per (plugin, field) tuple; the user can re-run via
a "regenerate suggestions" button (Phase 6 backlog).

Fallback: if the assistant LLM isn't configured OR the call fails, the
function returns an empty list — Step 3 still shows the plain textarea
so the user can type identifiers by hand.
"""

from __future__ import annotations

from typing import Any, Optional

import structlog
from pydantic import BaseModel, Field, model_validator

log = structlog.get_logger()


MAX_SUGGESTIONS_PER_FIELD = 8


class StreamSuggestion(BaseModel):
    """One candidate identifier for a stream field."""

    value: str = Field(
        description="The identifier value — no leading `r/` or `@` etc.",
    )
    rationale: str = Field(
        default="",
        description="One-line note on why this is relevant to the product.",
    )

    @model_validator(mode="before")
    @classmethod
    def _coerce_string(cls, data: Any) -> Any:
        """Robustness: if the LLM emits a bare string, treat it as the
        value with no rationale (same pattern as SuggestedSource)."""
        if isinstance(data, str):
            return {"value": data}
        return data


class StreamSuggestions(BaseModel):
    """Response contract — a list of candidates for one (plugin, field)."""

    suggestions: list[StreamSuggestion] = Field(default_factory=list)


def _contract():
    """Shared with profile_draft/taxonomy_proposal — assistant contract
    or None when unconfigured."""
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


def _profile_summary(profile_facts: dict[str, Any]) -> str:
    parts = [
        f"product: {profile_facts.get('display') or ''}",
        f"description: {profile_facts.get('description') or ''}",
    ]
    from pipeline.product import competitor_display_name
    for label, key in (
        ("aliases", "aliases"),
        ("scope_in", "scope_in"),
        ("scope_out", "scope_out"),
        ("competitors", "competitors"),
    ):
        vals = profile_facts.get(key) or []
        if key == "competitors":
            vals = [n for n in (competitor_display_name(v) for v in vals) if n]
        if vals:
            parts.append(f"{label}: {'; '.join(str(v) for v in vals)}")
    return "\n".join(parts)


def suggest_stream_identifiers(
    profile_facts: dict[str, Any],
    plugin_id: str,
    field_name: str,
    *,
    field_help: str = "",
    product_id_for_budget: Optional[str] = None,
) -> list[dict[str, str]]:
    """Return a list of {value, rationale} dicts, capped at
    MAX_SUGGESTIONS_PER_FIELD. Empty list on any failure — Step 3's plain
    textarea remains usable as a fallback path.

    Never raises. Token attribution stage: `assistant_stream_suggestions`.
    """
    from pipeline import prompt_templates
    from pipeline.token_usage import TokenContext, set_context

    contract = _contract()
    if contract is None:
        return []

    from pipeline.llm_contract import LLMCallSpec
    from pipeline.prompt_safety import TagKind, wrap_user_content

    user_prompt = (
        f"<profile>\n{_profile_summary(profile_facts)}\n</profile>\n"
        f"<plugin_id>{plugin_id}</plugin_id>\n"
        f"<field_name>{field_name}</field_name>\n"
        + (f"<field_help>{wrap_user_content(field_help, TagKind.USER_INPUT)}</field_help>\n"
           if field_help else "")
    )
    spec = LLMCallSpec(
        system=prompt_templates.get("assistant_stream_suggestions"),
        user=user_prompt,
        response_model=StreamSuggestions,
        cacheable_system=True,
        max_retries=1,
    )
    try:
        with set_context(TokenContext(
            stage="assistant_stream_suggestions",
            product_id=product_id_for_budget or "",
        )):
            result: StreamSuggestions = contract.call(spec)  # type: ignore[assignment]
    except Exception as e:
        log.warning("stream_suggestions.llm_failed",
                     plugin_id=plugin_id, field_name=field_name, error=str(e))
        return []

    # Dedup + cap; keep first occurrence's rationale.
    seen: dict[str, dict[str, str]] = {}
    for s in (result.suggestions or []):
        val = (s.value or "").strip()
        if not val or val in seen:
            continue
        seen[val] = {"value": val, "rationale": (s.rationale or "").strip()}
        if len(seen) >= MAX_SUGGESTIONS_PER_FIELD:
            break
    return list(seen.values())
