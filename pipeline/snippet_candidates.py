"""Snippet candidate discovery (POST_V1_PLAN §4.4-B).

Assistant LLM proposes 10-20 candidate snippets from the ingested
warehouse. Users review one-at-a-time with keyboard shortcuts on
`/products/<pid>/snippets/candidates` and either accept as
positive/negative snippets or skip.

Design points:

- **Stratified sample across `primary_area`**, max 3 per area, so the
  LLM isn't dominated by whichever area the user's product happens to
  ingest most heavily. Sample balance is surfaced to the UI so users
  can see the skew.
- **Structured output** via LLMResponseContract (§4.15). The LLM returns
  a ranked list with a one-line "why this is a good example" per item.
- **Assistant LLM only** (§4.8) — never the per-product classify model.
  Reason: candidates are a global-workflow concern, not per-run
  inference, and users benefit from a strong reasoning model here even
  if their classify model is cheap/local.
- **Budget check** before every LLM call, per D22.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import BaseModel, Field

from pipeline import assistant_llm, storage


# ---------------------------------------------------------------------------
# Structured LLM response
# ---------------------------------------------------------------------------


class CandidateSuggestion(BaseModel):
    """One LLM-ranked candidate snippet."""

    item_id: str = Field(description="The warehouse item id (verbatim from input).")
    polarity: str = Field(description="'positive_example' or 'negative_example'.")
    why: str = Field(description="One-line rationale, <= 150 chars.")


class CandidateSuggestions(BaseModel):
    """LLM output: ranked candidates picked from a stratified sample."""

    suggestions: list[CandidateSuggestion] = Field(
        description="Ranked best-to-worst. 10-20 items."
    )


SNIPPET_CANDIDATES_PROMPT = """\
You help a product team assemble labeled examples ("snippets") for their
feedback classifier. Pick 10-20 items from the sample below that would
teach the classifier the most.

Prefer items where:
  - The correct label is clear from the text alone (unambiguous)
  - The item exercises an interesting axis: an area, a content type
    boundary, a strong sentiment, a specific bug/feature request pattern
  - Diverse across areas — don't cluster on one topic
  - Mix positive_example (relevant, worth learning) and negative_example
    (off-topic, teaches the relevance gate)

For each pick, produce:
  - item_id: verbatim, must match one from the input
  - polarity: "positive_example" (in-scope for this product) or
              "negative_example" (out-of-scope, useful for the negative pool)
  - why: one line, <= 150 chars, why this item is a good example

Return JSON matching the CandidateSuggestions schema — no prose."""


# ---------------------------------------------------------------------------
# Stratified sampling from warehouse
# ---------------------------------------------------------------------------


@dataclass
class CandidatePool:
    """A stratified sample plus stats for the UI."""

    items: list[dict[str, Any]] = field(default_factory=list)
    per_area_counts: dict[str, int] = field(default_factory=dict)
    total_relevant: int = 0
    total_returned: int = 0


def sample_candidate_pool(
    product_id: str,
    *,
    max_per_area: int = 3,
    hard_ceiling: int = 100,
) -> CandidatePool:
    """Stratified sample from the warehouse: at most `max_per_area` items
    per primary_area, capped at `hard_ceiling` overall.

    Ordering: newest first within each area — recent items are more likely
    to reflect current product state.

    The query joins items → item_classifications so we can group by
    primary_area. Only relevant items are considered.
    """
    try:
        with storage.warehouse() as con:
            rows = con.execute(
                """
                SELECT i.id AS id,
                       i.source_display_name AS source_display_name,
                       i.title AS title,
                       i.body AS body,
                       i.url AS url,
                       i.author AS author,
                       i.created_at AS created_at,
                       COALESCE(ic.primary_area, '(unknown)') AS primary_area,
                       ic.summary AS summary,
                       ic.sentiment AS sentiment
                FROM items i
                LEFT JOIN item_classifications ic ON ic.item_id = i.id
                WHERE i.is_relevant = TRUE
                ORDER BY i.created_at DESC
                """,
            ).fetchall()
    except Exception:
        return CandidatePool()

    total_relevant = len(rows)
    per_area: dict[str, int] = {}
    picked: list[dict[str, Any]] = []
    for r in rows:
        area = r[7] or "(unknown)"
        if per_area.get(area, 0) >= max_per_area:
            continue
        picked.append({
            "id": r[0],
            "source_display_name": r[1],
            "title": r[2],
            "body": r[3],
            "url": r[4],
            "author": r[5],
            "created_at": r[6],
            "primary_area": area,
            "summary": r[8],
            "sentiment": r[9],
        })
        per_area[area] = per_area.get(area, 0) + 1
        if len(picked) >= hard_ceiling:
            break

    _ = product_id  # kept for API stability; warehouse is per-product already
    return CandidatePool(
        items=picked,
        per_area_counts=dict(sorted(per_area.items(), key=lambda kv: -kv[1])),
        total_relevant=total_relevant,
        total_returned=len(picked),
    )


# ---------------------------------------------------------------------------
# LLM ranking
# ---------------------------------------------------------------------------


def rank_candidates(pool: CandidatePool) -> list[CandidateSuggestion]:
    """Ask the assistant LLM to pick 10-20 best candidates from the pool.

    Requires the assistant LLM to be configured. Callers should also
    check `assistant_llm.is_within_budget(product_id)` before invoking
    to respect the per-product monthly cap (D22).

    Returns [] if the pool is empty or the LLM's response fails
    validation after the built-in retry-with-feedback in
    LLMResponseContract.
    """
    if not pool.items:
        return []

    from pipeline.llm_contract import LLMCallSpec

    system = SNIPPET_CANDIDATES_PROMPT
    user_lines = ["CANDIDATE ITEMS:"]
    for i in pool.items:
        excerpt = (i.get("body") or "")[:400].replace("\n", " ").strip()
        user_lines.append(
            f"- id: {i['id']}\n"
            f"  area: {i.get('primary_area') or '(unknown)'}\n"
            f"  source: {i.get('source_display_name') or ''}\n"
            f"  title: {i.get('title') or '(no title)'}\n"
            f"  body: {excerpt}"
        )

    try:
        client = assistant_llm.client()
    except RuntimeError:
        return []

    contract = _build_assistant_contract(client)
    try:
        result = contract.call(LLMCallSpec(
            system=system,
            user="\n".join(user_lines),
            response_model=CandidateSuggestions,
            cacheable_system=True,     # stable across calls; caches well on Anthropic
        ))
    except Exception:
        return []

    # Sanity: only keep suggestions whose item_id was actually in the pool.
    valid_ids = {i["id"] for i in pool.items}
    return [s for s in result.suggestions if s.item_id in valid_ids]


def _build_assistant_contract(inst) -> Any:
    """Wrap the assistant LLMClient in an LLMResponseContract without
    going through per-product routing lookup."""
    from pipeline.llm_contract import LLMResponseContract

    contract = LLMResponseContract.__new__(LLMResponseContract)
    contract._client = inst
    contract.role = "assistant"
    contract.model = inst.model
    contract.endpoint = inst.endpoint
    return contract


# ---------------------------------------------------------------------------
# Compose ranked suggestions with pool metadata for the UI
# ---------------------------------------------------------------------------


def rank_and_zip(pool: CandidatePool) -> list[dict[str, Any]]:
    """Return `[{item, suggestion}]` for the UI to iterate over.

    `suggestion` is None for pool items the LLM didn't pick — the UI can
    still show them under a "not ranked" section if desired.
    """
    ranking = rank_candidates(pool)
    by_id = {s.item_id: s for s in ranking}
    zipped = []
    # Ranked items first, preserving LLM's order
    for s in ranking:
        match = next((i for i in pool.items if i["id"] == s.item_id), None)
        if match:
            zipped.append({"item": match, "suggestion": s})
    # Then the rest (optional — the UI can hide)
    for i in pool.items:
        if i["id"] not in by_id:
            zipped.append({"item": i, "suggestion": None})
    return zipped
