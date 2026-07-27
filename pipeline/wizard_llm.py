"""Assistant-LLM helpers for the wizard (POST_V1_PLAN §4.3).

One structured-output helper per LLM-assist step. Each returns None when
the assistant LLM isn't configured — the wizard falls back to a plain
manual form with a "assistant LLM not configured" banner.

Each helper wraps the user-supplied description in <user_description>
tags to isolate prompt-injection attempts (per §4.3 security).
"""

from __future__ import annotations

from typing import Any, Optional

import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Shared client helper
# ---------------------------------------------------------------------------


def _assistant_contract():
    """Return an LLMResponseContract bound to the assistant LLM, or None
    if unconfigured. Shared shim for every wizard step."""
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


def _wrap_description(description: str) -> str:
    return f"<user_description>\n{description}\n</user_description>"


# ---------------------------------------------------------------------------
# Step 2: scope statement
# ---------------------------------------------------------------------------


class ScopeSuggestion(BaseModel):
    scope_in: str = Field(description="What's in scope for this product. 2-4 sentences.")
    scope_out: str = Field(description="What's out of scope. 2-4 sentences.")


# Wizard v1 system prompts are editable at Admin > Prompts. Read them
# from the templates helper at call time (not at import) so an admin
# override on disk takes effect immediately for the next call.
def _tpl(key: str) -> str:
    from pipeline import prompt_templates
    return prompt_templates.get(key)


def __getattr__(name):
    # Back-compat: existing imports like `from pipeline.wizard_llm import
    # SCOPE_SYSTEM` still work — but they resolve lazily from the config
    # so admin edits show up without a reimport.
    _MAP = {
        "SCOPE_SYSTEM":    "assistant_v1_scope",
        "TAXONOMY_SYSTEM": "assistant_v1_taxonomy",
        "VENDORS_SYSTEM":  "assistant_v1_vendors",
        "PROMPTS_SYSTEM":  "assistant_v1_prompts",
        "SNIPPETS_SYSTEM": "assistant_v1_snippets",
    }
    if name in _MAP:
        return _tpl(_MAP[name])
    raise AttributeError(name)


def suggest_scope(description: str) -> Optional[ScopeSuggestion]:
    contract = _assistant_contract()
    if contract is None:
        return None
    from pipeline.llm_contract import LLMCallSpec
    try:
        return contract.call(LLMCallSpec(
            system=_tpl("assistant_v1_scope"),
            user=_wrap_description(description),
            response_model=ScopeSuggestion,
            cacheable_system=True,
        ))
    except Exception as e:
        log.warning("wizard_scope_failed", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Step 3: taxonomy (areas + features)
# ---------------------------------------------------------------------------


class TaxonomyFeature(BaseModel):
    id: str = Field(description="Snake_case unique id within the area.")
    display: str = Field(description="Short human label, <= 40 chars.")


class TaxonomyArea(BaseModel):
    id: str = Field(description="Snake_case unique id.")
    display: str = Field(description="Short human label.")
    features: list[TaxonomyFeature] = Field(
        description="At least one feature per area.", min_length=1,
    )
    enabled: bool = True


class TaxonomySuggestion(BaseModel):
    areas: list[TaxonomyArea] = Field(
        description="3-8 top-level areas covering the product.", min_length=1,
    )


def suggest_taxonomy(
    description: str, scope_in: str = "", scope_out: str = "",
) -> Optional[TaxonomySuggestion]:
    contract = _assistant_contract()
    if contract is None:
        return None
    from pipeline.llm_contract import LLMCallSpec
    user = (
        _wrap_description(description)
        + f"\n<scope_in>{scope_in}</scope_in>"
        + f"\n<scope_out>{scope_out}</scope_out>"
    )
    try:
        return contract.call(LLMCallSpec(
            system=_tpl("assistant_v1_taxonomy"), user=user,
            response_model=TaxonomySuggestion,
            cacheable_system=True,
        ))
    except Exception as e:
        log.warning("wizard_taxonomy_failed", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Step 4: vendors
# ---------------------------------------------------------------------------


class VendorSuggestion(BaseModel):
    name: str = Field(description="Vendor / company name.")
    products: list[str] = Field(
        default_factory=list,
        description="Notable products from this vendor relevant to the topic.",
    )


class VendorsSuggestion(BaseModel):
    vendors: list[VendorSuggestion] = Field(
        description="Key vendors likely to appear in reports.", min_length=0,
    )


# VENDORS_SYSTEM is served lazily via __getattr__ (see top of file).


def suggest_vendors(
    description: str, areas: list[dict[str, Any]] | None = None,
) -> Optional[VendorsSuggestion]:
    contract = _assistant_contract()
    if contract is None:
        return None
    from pipeline.llm_contract import LLMCallSpec
    import json
    user = (
        _wrap_description(description)
        + "\n<areas>" + json.dumps(areas or [], ensure_ascii=False) + "</areas>"
    )
    try:
        return contract.call(LLMCallSpec(
            system=_tpl("assistant_v1_vendors"), user=user,
            response_model=VendorsSuggestion,
            cacheable_system=True,
        ))
    except Exception as e:
        log.warning("wizard_vendors_failed", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Step 5: prompt templates
# ---------------------------------------------------------------------------


class PromptTemplateSuggestion(BaseModel):
    relevance_system: str = Field(description="System message for the relevance gate.")
    relevance_template: str = Field(
        description="User-facing template. Include {title} {body} placeholders."
    )
    classify_system: str = Field(description="System message for the classifier.")
    classify_template: str = Field(
        description="User-facing template. Include {areas} {content_types} {title} {body} placeholders."
    )


# PROMPTS_SYSTEM is served lazily via __getattr__ (see top of file).


def suggest_prompts(
    description: str, scope_in: str = "", areas: list[dict[str, Any]] | None = None,
) -> Optional[PromptTemplateSuggestion]:
    contract = _assistant_contract()
    if contract is None:
        return None
    from pipeline.llm_contract import LLMCallSpec
    import json
    user = (
        _wrap_description(description)
        + f"\n<scope_in>{scope_in}</scope_in>"
        + "\n<areas>" + json.dumps(areas or [], ensure_ascii=False) + "</areas>"
    )
    try:
        return contract.call(LLMCallSpec(
            system=_tpl("assistant_v1_prompts"), user=user,
            response_model=PromptTemplateSuggestion,
            cacheable_system=True,
        ))
    except Exception as e:
        log.warning("wizard_prompts_failed", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Step 7: seed snippets
# ---------------------------------------------------------------------------


class SnippetSeed(BaseModel):
    polarity: str = Field(description="'positive_example' or 'negative_example'.")
    title: str = Field(description="Short title.")
    body: str = Field(description="Full body text — realistic length.")
    labels: dict[str, Any] = Field(
        default_factory=dict,
        description="{'areas': [...], 'content_types': [...], 'sentiment': -1..1}",
    )


class SnippetSeedSuggestion(BaseModel):
    snippets: list[SnippetSeed] = Field(
        description="5-10 seed snippets — mix of positive and negative.",
        min_length=1,
    )


# SNIPPETS_SYSTEM is served lazily via __getattr__ (see top of file).


def suggest_snippets(
    description: str, areas: list[dict[str, Any]] | None = None,
) -> Optional[SnippetSeedSuggestion]:
    contract = _assistant_contract()
    if contract is None:
        return None
    from pipeline.llm_contract import LLMCallSpec
    import json
    user = (
        _wrap_description(description)
        + "\n<areas>" + json.dumps(areas or [], ensure_ascii=False) + "</areas>"
    )
    try:
        return contract.call(LLMCallSpec(
            system=_tpl("assistant_v1_snippets"), user=user,
            response_model=SnippetSeedSuggestion,
            cacheable_system=True,
        ))
    except Exception as e:
        log.warning("wizard_snippets_failed", error=str(e))
        return None
