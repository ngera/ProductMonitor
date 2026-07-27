"""Render the product-facts block that gets injected into LLM prompts.

Product facts (aliases, confusables, in/out scope bullets, competitors) come
from the user via wizard v2 (or hand-edited product.yaml). They are per-product
context that helps the relevance and classify stages disambiguate posts. Because
the content is user-authored, it is wrapped in `<user_input>` tags with the
SYSTEM_PROMPT_SAFETY_PREAMBLE contract so the LLM treats it as data.

The block is intentionally short: no headers when empty, one labeled line per
non-empty field. `render_product_facts_block()` returns "" when the product has
no facts set, so callers can safely prepend unconditionally.
"""

from __future__ import annotations

from typing import Optional

from pipeline.prompt_safety import TagKind, wrap_user_content


def _line(label: str, values: list[str]) -> Optional[str]:
    if not values:
        return None
    return f"{label}: " + wrap_user_content("; ".join(values), TagKind.USER_INPUT)


def render_product_facts_block(product) -> str:
    """Return a short labeled block summarizing the product's facts.

    `product` is a ProductSpec (duck-typed — only reads .aliases,
    .not_to_be_confused_with, .scope_in, .scope_out). Returns "" when every
    fact field is empty so callers can prepend unconditionally without
    caring about back-compat with fact-less products.
    """
    lines: list[str] = []
    for label, values in (
        ("ALSO KNOWN AS", getattr(product, "aliases", []) or []),
        ("NOT THIS", getattr(product, "not_to_be_confused_with", []) or []),
        ("IN SCOPE", getattr(product, "scope_in", []) or []),
        ("OUT OF SCOPE", getattr(product, "scope_out", []) or []),
    ):
        line = _line(label, values)
        if line:
            lines.append(line)
    if not lines:
        return ""
    return "PRODUCT CONTEXT:\n" + "\n".join(lines)
