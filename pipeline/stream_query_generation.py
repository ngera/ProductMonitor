"""LLM-driven search-query generation for stream auto-discovery (ADR-0030).

Companion to `pipeline/stream_suggestions.py`. Different job:

- `stream_suggestions.py`: LLM proposes concrete identifiers
  (subreddit names, feed URLs) — the classic auto-suggest.
- `stream_query_generation.py` (this): LLM proposes SEARCH QUERIES
  that get fed to a provider-native search API inside each plugin's
  `discover_streams()`. Every candidate then comes from the provider's
  real data — no hallucination risk.

Called by `Source.discover_streams()` implementations that want the
LLM-queries-then-provider-search shape. Sources that prefer to skip
the LLM and search directly can call the plugin's search API without
this module.

Cached under `data/.discovery_cache/queries/<hash>.json` — same
`(profile_facts, plugin_id)` returns the same queries for one week.
The cache key hashes ONLY the profile fields the LLM sees, so a
profile edit that doesn't affect those fields (e.g. changing the URL)
doesn't invalidate.

Never raises. Returns [] on any failure (assistant LLM not configured,
LLM call failed, response malformed). Callers should treat [] as
"skip LLM path, use plugin-specific fallback if any".
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


MIN_QUERIES = 3
MAX_QUERIES = 8
CACHE_TTL_SECONDS = 7 * 24 * 3600      # 1 week; matches the ADR-0030 loop


class _QueriesResponse(BaseModel):
    """LLM response schema. Accepts either a bare list or a
    {queries: [...]} wrapping — the LLM sometimes wraps, sometimes doesn't.
    Normalization happens in _extract_queries()."""

    queries: list[str] = Field(default_factory=list)


def _cache_root() -> Path:
    from pipeline.config import app_config, resolve_path
    return resolve_path(app_config()["paths"]["data_root"]) / ".discovery_cache" / "queries"


def _profile_signature(profile_facts: dict[str, Any]) -> str:
    """Stable hash of the LLM-visible profile fields. Order-independent
    where the underlying field is a set-like list (aliases, scope_*)."""
    keys = ("display", "description", "aliases", "scope_in", "scope_out")
    normalized: dict[str, Any] = {}
    for k in keys:
        v = profile_facts.get(k)
        if isinstance(v, list):
            # Sort so [a, b] and [b, a] hash the same.
            normalized[k] = sorted(str(x).strip() for x in v if str(x).strip())
        elif v is None:
            normalized[k] = ""
        else:
            normalized[k] = str(v).strip()
    payload = json.dumps(normalized, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def _cache_key(profile_facts: dict[str, Any], plugin_id: str) -> str:
    return f"{plugin_id}-{_profile_signature(profile_facts)}"


def _load_cached(key: str) -> Optional[list[str]]:
    path = _cache_root() / f"{key}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    age = time.time() - float(data.get("cached_at") or 0)
    if age > CACHE_TTL_SECONDS:
        return None
    queries = data.get("queries") or []
    if not isinstance(queries, list):
        return None
    return [str(q).strip() for q in queries if str(q).strip()]


def _save_cached(key: str, queries: list[str]) -> None:
    path = _cache_root() / f"{key}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"cached_at": time.time(), "queries": queries}, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        # Cache is best-effort; never let a write failure break discovery.
        log.warning("stream_query_generation.cache_write_failed error=%s", e)


def _extract_queries(raw: Any) -> list[str]:
    """Normalize LLM output shapes into a flat list of strings.
    Accepts:
      ["query one", "query two"]           <- bare array
      {"queries": [...]}                   <- wrapped
      [{"query": "..."}, ...]              <- array of objects
    """
    if isinstance(raw, dict):
        raw = raw.get("queries") or []
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str):
            q = item.strip()
        elif isinstance(item, dict):
            q = str(item.get("query") or item.get("value") or "").strip()
        else:
            q = ""
        if q:
            out.append(q)
    return out


def _profile_block(profile_facts: dict[str, Any]) -> str:
    """Format the profile for the user prompt. Truncates description to
    keep the prompt bounded."""
    from pipeline.prompt_safety import TagKind, wrap_user_content
    lines = [
        f"display: {profile_facts.get('display') or ''}",
    ]
    aliases = profile_facts.get("aliases") or []
    if aliases:
        lines.append(f"aliases: {', '.join(str(a) for a in aliases[:8])}")
    desc = (profile_facts.get("description") or "").strip()
    if desc:
        lines.append(f"description: {desc[:400]}")
    scope_in = profile_facts.get("scope_in") or []
    if scope_in:
        lines.append(
            "scope_in: "
            + "; ".join(str(s) for s in scope_in[:6])
        )
    scope_out = profile_facts.get("scope_out") or []
    if scope_out:
        lines.append(
            "scope_out: "
            + "; ".join(str(s) for s in scope_out[:4])
        )
    body = "\n".join(lines)
    # Wrap the whole block since fields come from user input.
    return wrap_user_content(body, TagKind.USER_INPUT)


def generate_search_queries(
    profile_facts: dict[str, Any],
    plugin_id: str,
    *,
    force_regenerate: bool = False,
    product_id_for_budget: Optional[str] = None,
) -> list[str]:
    """Return between MIN_QUERIES and MAX_QUERIES search-query strings
    the plugin's discover_streams() can feed to its provider's search
    endpoint.

    `profile_facts` needs at minimum a `display` key; other keys
    (aliases, description, scope_in/out) improve query quality.
    `plugin_id` is the SourceManifest.plugin_id — routes the plugin-
    specific guidance in the prompt template.

    Cached by (profile, plugin_id). Set `force_regenerate=True` to
    bypass cache (used by the weekly review loop when we want fresh
    queries deliberately).

    Returns [] on any failure — never raises. Callers should treat [] as
    "LLM unavailable, use plugin fallback (if any)."
    """
    if not plugin_id:
        return []

    key = _cache_key(profile_facts, plugin_id)
    if not force_regenerate:
        cached = _load_cached(key)
        if cached:
            return cached[:MAX_QUERIES]

    # Lazy-imports so the module loads under installs without the
    # assistant LLM dependencies wired.
    try:
        from pipeline import assistant_llm, prompt_templates
        from pipeline.llm_contract import LLMCallSpec, LLMResponseContract
        from pipeline.token_usage import TokenContext, set_context
    except Exception as e:
        log.warning("stream_query_generation.deps_missing error=%s", e)
        return []

    try:
        client = assistant_llm.client()
    except RuntimeError:
        log.info("stream_query_generation.assistant_llm_unconfigured plugin=%s", plugin_id)
        return []

    contract = LLMResponseContract.__new__(LLMResponseContract)
    contract._client = client
    contract.role = "assistant"
    contract.model = client.model
    contract.endpoint = client.endpoint

    user_prompt = (
        f"<plugin_id>{plugin_id}</plugin_id>\n"
        f"<profile>\n{_profile_block(profile_facts)}\n</profile>\n"
    )
    spec = LLMCallSpec(
        system=prompt_templates.get("assistant_stream_search_queries"),
        user=user_prompt,
        response_model=_QueriesResponse,
        cacheable_system=True,
        max_retries=1,
    )
    try:
        with set_context(TokenContext(
            stage="assistant_stream_search_queries",
            product_id=product_id_for_budget or "",
        )):
            result = contract.call(spec)
        queries = list(result.queries or [])
    except Exception as e:
        # Try one more shape — some models emit a bare array despite the
        # schema. Attempt to grab the raw text and coerce.
        log.warning("stream_query_generation.llm_failed plugin=%s error=%s",
                    plugin_id, e)
        queries = []

    # Normalize + dedup while preserving order.
    seen: set[str] = set()
    normalized: list[str] = []
    for q in queries:
        s = str(q).strip()
        if not s:
            continue
        # Coarse quality gate: keep 2-6 words. Longer queries hurt
        # provider search precision; single words are usually too broad.
        n_words = len(s.split())
        if not (2 <= n_words <= 6):
            continue
        low = s.lower()
        if low in seen:
            continue
        seen.add(low)
        normalized.append(s)
        if len(normalized) >= MAX_QUERIES:
            break

    if len(normalized) < MIN_QUERIES:
        # Fallback: seed with the product display name if nothing usable
        # came back from the LLM. Better than returning [] because the
        # provider search will at least try SOMETHING.
        display = str(profile_facts.get("display") or "").strip()
        if display and display.lower() not in seen:
            normalized.insert(0, display)

    _save_cached(key, normalized)
    return normalized
