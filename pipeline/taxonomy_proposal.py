"""Taxonomy proposal service (wizard redesign Phase 5).

Given the wizard's mini-fetch corpus plus the confirmed profile facts,
propose a compact taxonomy (3-5 areas) grounded in real posts. Each area
gets a one-line description + keyword list + example item_ids from the
corpus (for the wizard review screen to render as evidence).

Feature-flagged by `assistant_llm_enabled` — when no LLM is configured or
the flag is off, we fall back to a "starter guess" derived from profile
facts alone, with `grounded=False` set on the returned proposal so the
UI can label it accordingly.

Per the plan we do NOT generate deep IN/OUT scope prose per feature; the
post-creation review→snippet→prompt-suggestion loop handles that.
"""

from __future__ import annotations

import random
from typing import Any, Optional

import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger()


MIN_AREAS = 3
MAX_AREAS = 5
MAX_KEYWORDS_PER_AREA = 8
MAX_EXAMPLES_PER_AREA = 3

# Cap on corpus tokens fed to the LLM. Rough heuristic: 40 items × ~200 tokens
# each ≈ 8k tokens body, plus prompt overhead.
MAX_CORPUS_ITEMS_FOR_PROMPT = 40


# ---------------------------------------------------------------------------
# Structured output (ADR-0007)
# ---------------------------------------------------------------------------


class ProposedArea(BaseModel):
    id: str = Field(description="snake_case unique id.")
    display: str = Field(description="Short human label.")
    description: str = Field(
        default="",
        description="ONE line describing the area — not a scope essay.",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Recognition keywords the classifier's regex pre-pass uses.",
    )
    rationale: str = Field(
        default="",
        description="Why this area — one line, shown in the review UI.",
    )
    example_item_ids: list[str] = Field(
        default_factory=list,
        description="Up to 3 corpus item ids exemplifying the area.",
    )


class TaxonomyProposal(BaseModel):
    areas: list[ProposedArea] = Field(
        description=f"Between {MIN_AREAS} and {MAX_AREAS} areas.",
        min_length=1,
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


# System prompt is editable at Admin > Prompts (key `assistant_taxonomy_proposal`).
def _taxonomy_system_prompt() -> str:
    from pipeline import prompt_templates
    return prompt_templates.get("assistant_taxonomy_proposal")


def __getattr__(name):
    if name == "TAXONOMY_PROPOSAL_SYSTEM":
        return _taxonomy_system_prompt()
    raise AttributeError(name)


# ---------------------------------------------------------------------------
# Grounded proposal — the LLM-driven path
# ---------------------------------------------------------------------------


def _corpus_excerpt(corpus: list[dict[str, Any]]) -> str:
    """Take the corpus down to a reasonable prompt size."""
    picked = corpus[:MAX_CORPUS_ITEMS_FOR_PROMPT]
    lines: list[str] = []
    for it in picked:
        title = (it.get("title") or "").strip()
        body = (it.get("body") or "").strip()
        item_id = it.get("id") or ""
        # Truncate bodies aggressively; the LLM needs signal, not the whole post.
        lines.append(f"- id={item_id} | {title} | {body[:200]}")
    return "\n".join(lines)


def _profile_summary(profile_facts: dict[str, Any]) -> str:
    parts = []
    for label, key in (
        ("aliases", "aliases"),
        ("scope_in", "scope_in"),
        ("scope_out", "scope_out"),
        ("competitors", "competitors"),
    ):
        vals = profile_facts.get(key) or []
        if vals:
            parts.append(f"{label}: {'; '.join(str(v) for v in vals)}")
    parts.insert(0, f"description: {profile_facts.get('description') or ''}")
    parts.insert(0, f"display: {profile_facts.get('display') or ''}")
    return "\n".join(parts)


def _contract():
    """Same shim used by profile_draft — an assistant-LLM contract or None."""
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


def _validate(proposal: TaxonomyProposal, corpus_ids: set[str]) -> TaxonomyProposal:
    """Trim / dedupe / clamp the LLM's proposal so it fits our contract."""
    seen: set[str] = set()
    clean: list[ProposedArea] = []
    for a in (proposal.areas or []):
        if not a.id or a.id in seen:
            continue
        seen.add(a.id)
        a.keywords = list(dict.fromkeys(a.keywords or []))[:MAX_KEYWORDS_PER_AREA]
        a.example_item_ids = [i for i in (a.example_item_ids or []) if i in corpus_ids][
            :MAX_EXAMPLES_PER_AREA
        ]
        clean.append(a)
    proposal.areas = clean[:MAX_AREAS]
    return proposal


def propose_taxonomy(
    profile_facts: dict[str, Any],
    corpus: list[dict[str, Any]],
    *,
    product_id_for_budget: Optional[str] = None,
) -> tuple[Optional[TaxonomyProposal], bool]:
    """Return (proposal, grounded).

    `grounded=True` when the proposal was drafted from the LLM against real
    corpus items. `grounded=False` when we fell back to a starter guess
    (no LLM configured OR no corpus). The returned proposal is never None
    unless every fallback path failed.
    """
    from pipeline.token_usage import TokenContext, set_context
    if not corpus:
        return _starter_guess(profile_facts), False

    contract = _contract()
    if contract is None:
        return _starter_guess(profile_facts), False

    from pipeline.llm_contract import LLMCallSpec
    from pipeline.prompt_safety import TagKind, wrap_user_content

    user = (
        f"<profile>{_profile_summary(profile_facts)}</profile>\n"
        f"<sample_posts>\n{wrap_user_content(_corpus_excerpt(corpus), TagKind.SCRAPED_CONTENT)}\n"
        f"</sample_posts>"
    )
    spec = LLMCallSpec(
        system=_taxonomy_system_prompt(),
        user=user,
        response_model=TaxonomyProposal,
        cacheable_system=True,
        max_retries=1,
    )
    ctx = TokenContext(
        stage="assistant_wizard_taxonomy",
        product_id=product_id_for_budget or "",
    )
    try:
        with set_context(ctx):
            proposal: TaxonomyProposal = contract.call(spec)  # type: ignore[assignment]
    except Exception as e:
        log.warning("taxonomy_proposal.llm_failed", error=str(e))
        return _starter_guess(profile_facts), False

    corpus_ids = {it.get("id") for it in corpus if it.get("id")}
    proposal = _validate(proposal, corpus_ids)
    if not proposal.areas:
        return _starter_guess(profile_facts), False
    return proposal, True


# ---------------------------------------------------------------------------
# Starter guess — the fallback when we can't call the LLM
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")
    return slug or "area"


def _starter_guess(profile_facts: dict[str, Any]) -> TaxonomyProposal:
    """A deterministic 3-area guess built from the profile facts alone.

    Not a great taxonomy — but the wizard has to render something, and this
    beats a blank form. The `grounded=False` flag on the return tuple lets
    the UI label this "starter guess — edit before running".
    """
    display = profile_facts.get("display") or "Product"
    scope_in = list(profile_facts.get("scope_in") or [])
    aliases = list(profile_facts.get("aliases") or [])

    areas: list[ProposedArea] = []
    # 1) general — always safe fallback.
    areas.append(ProposedArea(
        id="general",
        display=f"{display} — general",
        description=f"Any public discussion about {display}.",
        keywords=[display, *aliases[:5]],
        rationale="Fallback area for posts that don't fit a narrower bucket.",
    ))
    # 2) bugs (implicit — every product needs it).
    areas.append(ProposedArea(
        id="bugs",
        display="Bugs",
        description="Reports of broken behavior.",
        keywords=["bug", "broken", "crash", "error", "fails"],
        rationale="A dedicated bug bucket makes triage easier.",
    ))
    # 3) requests (feature requests, feedback).
    areas.append(ProposedArea(
        id="requests",
        display="Feature requests",
        description="Suggestions and feature asks.",
        keywords=["feature", "request", "wish", "please add", "would love"],
        rationale="Groups the 'nice to have' backlog.",
    ))
    # 4+) one per scope_in bullet, up to MAX_AREAS.
    for bullet in scope_in[: MAX_AREAS - len(areas)]:
        slug = _slugify(bullet)[:32] or f"area-{len(areas)}"
        areas.append(ProposedArea(
            id=slug,
            display=bullet[:40],
            description=bullet,
            keywords=[bullet],
            rationale="Derived from your in-scope bullets.",
        ))
    return TaxonomyProposal(areas=areas[:MAX_AREAS])


# ---------------------------------------------------------------------------
# Serialization — proposal → taxonomy.yaml shape
# ---------------------------------------------------------------------------


def to_taxonomy_yaml(proposal: TaxonomyProposal, version: str) -> dict[str, Any]:
    """Turn a proposal into the shape `load_product()` expects: areas each
    with at least one feature. Each area gets a single feature seeded from
    the area's own description (users refine in the taxonomy editor later)."""
    areas_out: list[dict[str, Any]] = []
    for a in proposal.areas:
        areas_out.append({
            "id": a.id,
            "display": a.display,
            "enabled": True,
            "keywords": a.keywords or [],
            "entity_type_hint": [],
            "features": [
                {
                    "id": a.id,
                    "display": a.display,
                    "description": a.description or a.display,
                }
            ],
        })
    return {"version": version, "areas": areas_out}
