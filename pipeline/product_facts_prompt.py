"""Render product-facts / relevance context blocks for LLM prompts.

Product facts (aliases, confusables, in/out scope) and taxonomy themes
(feature display + description) are user-authored. Content is wrapped in
``<user_input>`` tags with the SYSTEM_PROMPT_SAFETY_PREAMBLE contract so the
LLM treats it as data.

Also builds the deterministic relevance pre-gate needle list from the same
context (brand + theme/scope phrases) so the gate tracks authored product
context rather than a parallel keyword file.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from pipeline.prompt_safety import TagKind, wrap_user_content

# Tokens that add noise when extracted from feature descriptions for the
# pre-gate (length filter alone is not enough).
_STOPWORDS = frozenset({
    "about", "above", "after", "again", "against", "among", "and", "are",
    "because", "been", "before", "being", "below", "between", "both", "but",
    "can", "does", "doing", "down", "during", "each", "for", "from", "further",
    "had", "has", "have", "having", "here", "how", "into", "itself", "just",
    "more", "most", "not", "only", "other", "our", "out", "over", "own",
    "same", "should", "some", "such", "than", "that", "the", "their", "then",
    "there", "these", "they", "this", "those", "through", "too", "under",
    "until", "very", "was", "were", "what", "when", "where", "which", "while",
    "who", "whom", "why", "will", "with", "you", "your", "users", "user",
    "content", "issues", "issue", "feature", "features", "general", "including",
    "support", "options", "functionality", "integration",
})

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]{3,}", re.I)


def _line(label: str, values: list[str]) -> Optional[str]:
    if not values:
        return None
    return f"{label}: " + wrap_user_content("; ".join(values), TagKind.USER_INPUT)


def _competitor_names(product) -> list[str]:
    """Flatten `product.competitors` — which may hold plain strings or
    ADR-0019 rich `{name, aliases, ...}` dicts — into a list of names."""
    out: list[str] = []
    for c in (getattr(product, "competitors", None) or []):
        if isinstance(c, str):
            name = c.strip()
        elif isinstance(c, dict):
            name = str(c.get("name") or "").strip()
        else:
            name = ""
        if name:
            out.append(name)
    return out


def render_product_facts_block(product) -> str:
    """Return a short labeled block summarizing the product's facts.

    `product` is a ProductSpec (duck-typed — only reads .aliases,
    .not_to_be_confused_with, .competitors, .scope_in, .scope_out). Returns
    "" when every fact field is empty so callers can prepend unconditionally
    without caring about back-compat with fact-less products.

    Labels spell out the semantic role so weak LLMs follow them as rules,
    not just context. Competitors and confusables are framed as
    "not-this-product" hints; scope_out is framed as a hard reject rule.
    """
    lines: list[str] = []
    for label, values in (
        ("ALSO KNOWN AS", getattr(product, "aliases", []) or []),
        ("NOT THIS PRODUCT (different product with similar name)",
         getattr(product, "not_to_be_confused_with", []) or []),
        ("COMPETITORS (different products — mentioning them ALONE is NOT this product)",
         _competitor_names(product)),
        ("IN SCOPE (topics that count as relevant when tied to the product)",
         getattr(product, "scope_in", []) or []),
        ("OUT OF SCOPE (hard reject — items primarily about these are NOT relevant)",
         getattr(product, "scope_out", []) or []),
    ):
        line = _line(label, values)
        if line:
            lines.append(line)
    if not lines:
        return ""
    return "PRODUCT CONTEXT:\n" + "\n".join(lines)


def build_relevance_context(product) -> str:
    """Full relevance context: product facts + enabled theme/feature catalog.

    Theme descriptions are the authored recognition text (same copy classify
    uses). Returns "" when there are no facts and no enabled features — the
    product display alone is already in the prompt via ``{product_display}``.
    """
    parts: list[str] = []

    facts = render_product_facts_block(product)
    if facts:
        # Drop the "PRODUCT CONTEXT:" header when composing the larger block.
        body = facts.split("\n", 1)[-1] if facts.startswith("PRODUCT CONTEXT:") else facts
        parts.append(body)

    theme_lines: list[str] = []
    enabled_areas = []
    if hasattr(product, "enabled_areas") and callable(product.enabled_areas):
        enabled_areas = product.enabled_areas()
    else:
        tax = getattr(product, "taxonomy", None) or {}
        enabled_areas = [
            a for a in (tax.get("areas") or []) if a.get("enabled", True)
        ]

    for area in enabled_areas:
        area_display = (area.get("display") or area.get("id") or "").strip()
        for feat in (area.get("features") or []):
            feat_display = (feat.get("display") or area_display or feat.get("id") or "").strip()
            desc = (feat.get("description") or "").strip()
            if not feat_display and not desc:
                continue
            label = feat_display or area_display
            if desc:
                theme_lines.append(
                    f"- {wrap_user_content(label, TagKind.USER_INPUT)}: "
                    f"{wrap_user_content(desc, TagKind.USER_INPUT)}"
                )
            else:
                theme_lines.append(f"- {wrap_user_content(label, TagKind.USER_INPUT)}")

    if theme_lines:
        parts.append("THEMES:\n" + "\n".join(theme_lines))

    if not parts:
        return ""

    display = (getattr(product, "display", None) or getattr(product, "id", "") or "").strip()
    if display:
        parts.insert(0, f"PRODUCT: {wrap_user_content(display, TagKind.USER_INPUT)}")
    return "\n".join(parts)


def _brand_needles(product) -> list[str]:
    needles: list[str] = []
    for raw in (
        [getattr(product, "display", None), getattr(product, "id", None)]
        + list(getattr(product, "aliases", None) or [])
    ):
        s = (raw or "").strip().lower()
        if not s:
            continue
        needles.append(s)
        needles.append(s.replace("-", " "))
        needles.append(s.replace(" ", ""))
        needles.append(s.replace("-", ""))
    url = (getattr(product, "url", None) or "").strip()
    if url:
        try:
            host = (urlparse(url).hostname or "").lower()
            if host.startswith("www."):
                host = host[4:]
            if host:
                needles.append(host)
                needles.append(host.split(".")[0])
        except Exception:
            pass
    return needles


def _normalize_phrase(s: str) -> str:
    s = (s or "").lower().strip()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\s-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _phrases_and_tokens_from_text(text: str) -> list[str]:
    """Multi-word displays kept whole; significant tokens length ≥ 5."""
    out: list[str] = []
    norm = _normalize_phrase(text)
    if not norm:
        return out
    if " " in norm and len(norm) >= 5:
        out.append(norm)
    for m in _WORD_RE.finditer(norm):
        tok = m.group(0).lower().strip("'")
        if len(tok) < 5 or tok in _STOPWORDS:
            continue
        out.append(tok)
    return out


def relevance_context_needles(product) -> list[str]:
    """Needles for the relevance pre-gate, derived from built product context."""
    needles: list[str] = list(_brand_needles(product))

    for scope in (getattr(product, "scope_in", None) or []):
        needles.extend(_phrases_and_tokens_from_text(str(scope)))

    enabled_areas: list[dict[str, Any]] = []
    if hasattr(product, "enabled_areas") and callable(product.enabled_areas):
        enabled_areas = product.enabled_areas()
    else:
        tax = getattr(product, "taxonomy", None) or {}
        enabled_areas = [
            a for a in (tax.get("areas") or []) if a.get("enabled", True)
        ]

    for area in enabled_areas:
        needles.extend(_phrases_and_tokens_from_text(area.get("display") or ""))
        for feat in (area.get("features") or []):
            needles.extend(_phrases_and_tokens_from_text(feat.get("display") or ""))
            needles.extend(_phrases_and_tokens_from_text(feat.get("description") or ""))
            fid = (feat.get("id") or "").strip().lower()
            if fid:
                needles.append(fid.replace("_", " ").replace("-", " "))
                needles.append(fid.replace("_", "").replace("-", ""))

    seen: set[str] = set()
    out: list[str] = []
    for n in needles:
        n = (n or "").strip().lower()
        if len(n) < 3 or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


def text_mentions_product_context(title: str, body: str, product) -> bool:
    """True when title/body overlaps brand or theme/scope context needles."""
    text = f"{title or ''}\n{body or ''}".lower()
    if not text.strip():
        return False
    return any(n in text for n in relevance_context_needles(product))


def text_mentions_product_brand(title: str, body: str, product) -> bool:
    """Strict brand-only overlap check for media_coverage sources.

    General news articles frequently share vocabulary with our scope_in /
    theme catalog (words like "database", "cloud", "backend") without ever
    naming the product. The looser `text_mentions_product_context` lets
    those through and forces the weak local LLM to make a call. This
    stricter gate requires the product's display name or an alias to
    appear literally — enough to catch RSS/media noise before we spend an
    LLM call.
    """
    text = f"{title or ''}\n{body or ''}".lower()
    if not text.strip():
        return False
    needles = _brand_needles(product)
    return any(n and n in text for n in needles)
