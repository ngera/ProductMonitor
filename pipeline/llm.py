"""LLM client wrapper for Foundry Local (DESIGN.md §4.4.1, §4.6, §10/§11.1).

Uses the `openai` SDK pointed at Foundry Local's OpenAI-compatible endpoint.
Prefers guided/JSON-schema-constrained decoding; falls back to a single
validate-and-repair call when it isn't available.

`replay://` endpoints (ADR-0010, POST_V1 §4.12) bypass the network entirely
and return recorded responses keyed by a hash of (role, system, user). Used
by the `product-monitor demo` bundle and by CI tests. Prompt drift naturally
invalidates the replay (hash changes) so stale bundles fail loudly.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional, Type, TypeVar

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from pipeline.config import app_config

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    pass


# ---------------------------------------------------------------------------
# Replay adapter (ADR-0010, POST_V1_PLAN §4.12)
# ---------------------------------------------------------------------------


def _is_replay_endpoint(endpoint: str) -> bool:
    return (endpoint or "").startswith("replay://")


def _is_ollama_endpoint(endpoint: str) -> bool:
    """Ollama's OpenAI-compat layer does not enforce json_schema / strict
    guided decoding — it often returns prose or empty content instead.
    `response_format: json_object` is the reliable local path (ADR-0007)."""
    e = (endpoint or "").lower()
    return "11434" in e or "ollama" in e


def _replay_path_from_endpoint(endpoint: str) -> Path:
    """`replay://demo` → data/demo/llm_replay.jsonl (bundle default).
    `replay:///abs/path/to/file.jsonl` → that path verbatim.
    """
    tail = endpoint[len("replay://") :]
    if tail in ("", "demo"):
        # Default bundle location — the demo command copies it here.
        return Path(__file__).resolve().parent.parent / "data" / "demo" / "llm_replay.jsonl"
    return Path(tail)


def _prompt_key(role: str, system: str, user: str) -> str:
    """Deterministic hash used to look up recorded responses. Any prompt
    edit changes the key, so a stale replay bundle fails loudly rather than
    silently serving wrong output."""
    h = hashlib.sha256()
    h.update(role.encode("utf-8"))
    h.update(b"\0")
    h.update(system.encode("utf-8"))
    h.update(b"\0")
    h.update(user.encode("utf-8"))
    return h.hexdigest()[:16]


class _ReplayStore:
    """Lazy-loaded JSONL of recorded responses. Each line is one record:
        {"key": "<prompt_key>", "role": "classify", "stage": "classify",
         "item_id": "hn:12345", "content": "<raw string returned>",
         "usage": {"prompt_tokens": 500, "completion_tokens": 120}}
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._by_key: Optional[dict[str, dict]] = None

    def _load(self) -> dict[str, dict]:
        if self._by_key is not None:
            return self._by_key
        out: dict[str, dict] = {}
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    key = rec.get("key")
                    if key:
                        out[key] = rec
        self._by_key = out
        return out

    def lookup(self, key: str) -> Optional[dict]:
        return self._load().get(key)


_LOCAL_HINTS = ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal")
_PROVIDER_ENV_HINTS = (
    # (substring of endpoint, env var). Order matters when substrings could
    # overlap — more-specific matches first.
    ("api.anthropic.com", "ANTHROPIC_API_KEY"),
    ("anthropic.com",     "ANTHROPIC_API_KEY"),
    ("api.openai.com",    "OPENAI_API_KEY"),
    ("openai.com",        "OPENAI_API_KEY"),
    ("openai.azure.com",  "AZURE_OPENAI_API_KEY"),
    ("googleapis.com",    "GOOGLE_API_KEY"),
    ("openrouter.ai",     "OPENROUTER_API_KEY"),
    ("groq.com",          "GROQ_API_KEY"),
    ("together.xyz",      "TOGETHER_API_KEY"),
    ("together.ai",       "TOGETHER_API_KEY"),
    ("fireworks.ai",      "FIREWORKS_API_KEY"),
    ("deepinfra.com",     "DEEPINFRA_API_KEY"),
    ("perplexity.ai",     "PERPLEXITY_API_KEY"),
    ("mistral.ai",        "MISTRAL_API_KEY"),
    ("cohere.com",        "COHERE_API_KEY"),
    ("cohere.ai",         "COHERE_API_KEY"),
)


def _is_anthropic_endpoint(endpoint: str) -> bool:
    return "anthropic.com" in (endpoint or "").lower()


def cacheable_content(text: str, endpoint: str) -> Any:
    """Wrap `text` so it becomes a cacheable prompt block on providers that
    support it. Returns a plain string on providers that don't — so callers
    can pass the result straight into a chat message `content` field.

    Anthropic — via their OpenAI-compat endpoint — recognises the
    array-of-blocks content shape with a `cache_control` marker on each
    block. Ephemeral cache: 5-min TTL, useful for tight-loop stages
    (classify) that reuse the same taxonomy across every item.

    Reference: https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching
    """
    if _is_anthropic_endpoint(endpoint):
        return [
            {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
        ]
    return text


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a Pydantic-generated JSON schema so it satisfies OpenAI /
    Anthropic strict-mode requirements.

    Both providers reject `strict: true` schemas that:
      1. omit `"additionalProperties": false` on any object type; and
      2. have a `properties` object where the `required` array doesn't list
         *every* property (strict mode has no notion of "optional field").

    We walk the schema recursively, patch every object type, and drop
    provider-unfriendly keywords (`default`, `minLength`, `maxLength`,
    `minItems`, `maxItems`, `pattern`, `format` on non-string types) that
    OpenAI strict mode doesn't accept. Fields with defaults become required
    but the LLM is free to reproduce the default value — this loses "the
    LLM may omit this" semantics in exchange for the schema being accepted.
    """
    UNSUPPORTED_KEYS = {
        "minLength", "maxLength", "minItems", "maxItems", "pattern",
        "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
        "multipleOf", "format",
    }

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            # Strip default AT THIS LEVEL — strict mode disallows.
            node.pop("default", None)
            for k in list(node.keys()):
                if k in UNSUPPORTED_KEYS:
                    node.pop(k, None)
            if node.get("type") == "object":
                # ALL object types need additionalProperties=false, even
                # bare `{"type": "object"}` emitted for `dict`-typed fields
                # (e.g. `stream_config: dict`). Without this hole, Anthropic
                # and OpenAI strict mode both reject the schema.
                node["additionalProperties"] = False
                if "properties" in node:
                    node["required"] = list(node["properties"].keys())
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        return node

    return _walk(schema)


def _resolve_api_key(cfg: dict[str, Any], env: dict[str, str]) -> str:
    """Pick the right API key for an OpenAI-compatible endpoint.

    Precedence:
    1. explicit `api_key_env` in the per-stage routing config
    2. provider inferred from the endpoint URL (Anthropic / OpenAI / etc.)
    3. local endpoint → return a placeholder so the OpenAI SDK is happy
       (Ollama, Foundry Local, vLLM, LM Studio all ignore the bearer token)
    """
    explicit = cfg.get("api_key_env")
    if explicit:
        return env.get(explicit, "") or "not-needed-for-local"

    endpoint = (cfg.get("endpoint") or "").lower()
    for substr, env_name in _PROVIDER_ENV_HINTS:
        if substr in endpoint:
            return env.get(env_name, "") or "not-needed-for-local"

    if any(h in endpoint for h in _LOCAL_HINTS):
        return "not-needed-for-local"

    # Unknown remote endpoint — caller should set api_key_env explicitly.
    return env.get("LLM_API_KEY", "") or "not-needed-for-local"


class LLMClient:
    """One client per LLM role ('relevance' | 'classify')."""

    def __init__(self, role: str) -> None:
        import os

        # Phase 0: prefer per-topic routing from topics/<id>/llm_routing.yaml;
        # fall back to legacy app.yaml `llm:` block for installs that haven't
        # migrated yet.
        cfg = None
        try:
            from pipeline.config import current_product
            cfg = (current_product().llm_routing or {}).get(role)
        except Exception:
            cfg = None
        if not cfg:
            cfg = app_config().get("llm", {}).get(role)
        if not cfg:
            raise LLMError(
                f"No LLM config found for role '{role}'. Add it to the product's "
                f"llm_routing.yaml or to config/app.yaml under llm.{role}."
            )

        self.role = role
        self.cfg = cfg
        self.model = cfg["model"]
        self.endpoint = cfg["endpoint"]

        # Replay endpoint short-circuits network I/O — skip the OpenAI client
        # entirely so `uvx product-monitor demo` runs with zero deps beyond
        # what a fresh Python install ships with.
        self._replay: Optional[_ReplayStore] = None
        if _is_replay_endpoint(self.endpoint):
            self._replay = _ReplayStore(_replay_path_from_endpoint(self.endpoint))
            self._client = None
            return

        from openai import OpenAI

        api_key = _resolve_api_key(cfg, os.environ)
        # Trailing slash is REQUIRED by Anthropic's OpenAI-compat layer —
        # without it httpx.URL.join drops the `/v1` segment (RFC 3986
        # relative-reference behavior) and requests land on the wrong path.
        # Adding it unconditionally is safe for every provider we support.
        base_url = cfg["endpoint"]
        if base_url and not base_url.endswith("/"):
            base_url = base_url + "/"
        self._client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=cfg.get("timeout_seconds", 60),
            max_retries=cfg.get("max_retries", 3),
        )

    # --- health -------------------------------------------------------------

    def health_check(self) -> bool:
        """Tiny 1-token chat completion against the configured model.

        Models endpoints aren't a reliable probe: Anthropic's /v1/models
        rejects Bearer auth even though /v1/chat/completions accepts it,
        so a models.list() probe would say 'unreachable' for a perfectly
        working Claude setup. Hitting chat.completions exercises the same
        path the relevance + classify stages will use — endpoint, auth,
        model id, and quota — so a pass here actually means runs will
        proceed. Cost: ~1 input + 1 output token (sub-cent on all providers).
        """
        if self._replay is not None:
            # Replay bundle exists iff the file is present. If someone points
            # at replay:// without a bundle, fail health so the pipeline still
            # gracefully skips LLM stages (see run.py _llm_reachable).
            return self._replay.path.exists()
        try:
            # `max_tokens` (not `max_completion_tokens`) — Anthropic's
            # OpenAI-compat layer only recognizes the classic name; OpenAI
            # itself still accepts it (marked deprecated but functional).
            # Using `max_completion_tokens` here silently rejected on
            # Anthropic and made every Claude setup look unreachable.
            self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return True
        except Exception as e:
            log.warning(
                "llm_health_check_failed",
                role=self.role,
                endpoint=self.endpoint,
                model=self.model,
                error=str(e),
            )
            return False

    # --- structured generation ----------------------------------------------

    def structured(
        self,
        system: str,
        user: str,
        schema_model: Type[T],
        *,
        guided: Optional[bool] = None,
    ) -> T:
        """Return a validated instance of schema_model.

        Tries guided decoding (json_schema response_format); on failure or if
        disabled, parses free-form JSON and runs one repair attempt.
        Ollama gets json_object instead of json_schema — it does not enforce
        schemas and empty/prose replies were skipping relevance items.
        """
        guided = self.cfg.get("use_guided_decoding", False) if guided is None else guided
        # Ollama ignores json_schema; force the json_object path.
        if _is_ollama_endpoint(self.endpoint):
            guided = False
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        raw_text: str = ""
        if guided:
            try:
                raw_text = self._call(messages, schema_model=schema_model)
                return schema_model.model_validate_json(
                    _prepare_structured_json(raw_text, schema_model)
                )
            except (ValidationError, json.JSONDecodeError) as e:
                log.warning("guided_validation_failed", role=self.role, error=str(e))
            except Exception as e:
                # endpoint may not support json_schema response_format
                log.warning("guided_unsupported", role=self.role, error=str(e))

        # Free-form / json_object path
        if not raw_text:
            raw_text = self._call(
                messages,
                schema_model=None,
                force_json_object=_is_ollama_endpoint(self.endpoint),
            )
        try:
            return schema_model.model_validate_json(
                _prepare_structured_json(raw_text, schema_model)
            )
        except (ValidationError, json.JSONDecodeError) as e:
            return self._repair(messages, schema_model, raw_text, str(e))

    def _repair(
        self, messages: list[dict[str, str]], schema_model: Type[T], bad: str, err: str
    ) -> T:
        attempts = self.cfg.get("fallback_repair_attempts", 1)
        for _ in range(attempts):
            # Ollama's chat template crashes on an empty assistant turn
            # (`can't evaluate field ToolCalls`). Skip it when there was
            # nothing useful to echo back.
            repair_msg = list(messages)
            if (bad or "").strip():
                repair_msg.append({"role": "assistant", "content": bad})
            repair_msg.append({
                "role": "user",
                "content": (
                    f"Your previous output failed validation: {err}\n"
                    "Return ONLY a single JSON object matching this shape:\n"
                    f"{_minimal_json_example(schema_model)}\n"
                    "No prose, no markdown."
                ),
            })
            text = self._call(
                repair_msg,
                schema_model=None,
                force_json_object=_is_ollama_endpoint(self.endpoint),
            )
            try:
                return schema_model.model_validate_json(
                    _prepare_structured_json(text, schema_model)
                )
            except (ValidationError, json.JSONDecodeError) as e:
                err = str(e)
                bad = text
        raise LLMError(f"{schema_model.__name__} validation failed after repair: {err}")

    def _call(
        self,
        messages: list[dict[str, str]],
        schema_model: Optional[Type[BaseModel]],
        *,
        force_json_object: bool = False,
    ) -> str:
        # Replay path: look up recorded response by prompt hash and record
        # the replayed token counts under the current attribution context.
        # getattr default keeps test stubs that skip __init__ working.
        replay = getattr(self, "_replay", None)
        if replay is not None:
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            user = next((m["content"] for m in messages if m["role"] == "user"), "")
            key = _prompt_key(self.role, str(system), str(user))
            rec = replay.lookup(key)
            if rec is None:
                raise LLMError(
                    f"replay miss for role={self.role!r} key={key}. Rebuild the "
                    f"demo bundle after prompt changes (see scripts/capture_demo.py)."
                )
            usage = rec.get("usage") or {}
            try:
                from pipeline import token_usage as _tu
                _tu.record_usage(
                    endpoint=self.endpoint,
                    model=self.model,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
                )
            except Exception:
                pass
            return rec.get("content") or ""

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.cfg.get("temperature", 0),
        }
        if self.cfg.get("seed") is not None:
            kwargs["seed"] = self.cfg["seed"]
        if schema_model is not None and not _is_ollama_endpoint(self.endpoint):
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_model.__name__,
                    "schema": _strict_schema(schema_model.model_json_schema()),
                    "strict": True,
                },
            }
        elif force_json_object or (
            schema_model is not None and _is_ollama_endpoint(self.endpoint)
        ):
            # Ollama: json_object returns real JSON; json_schema does not.
            kwargs["response_format"] = {"type": "json_object"}
        resp = self._client.chat.completions.create(**kwargs)

        # POST_V1_PLAN §4.11 — token attribution. Best-effort: telemetry
        # never blocks or crashes the pipeline call.
        try:
            usage = getattr(resp, "usage", None)
            if usage is not None:
                prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                # Anthropic returns cache_read_input_tokens on their SDK; on the
                # OpenAI-compat pass-through the field name varies. Try common
                # names before giving up.
                cached_input_tokens = 0
                for attr in ("cache_read_input_tokens", "cached_input_tokens", "prompt_cache_hit_tokens"):
                    val = getattr(usage, attr, None)
                    if val is not None:
                        cached_input_tokens = int(val)
                        break
                if cached_input_tokens == 0:
                    # OpenAI-compat nests cached tokens under prompt_tokens_details
                    details = getattr(usage, "prompt_tokens_details", None)
                    if details is not None:
                        val = getattr(details, "cached_tokens", None)
                        if val is not None:
                            cached_input_tokens = int(val)
                from pipeline import token_usage as _tu
                _tu.record_usage(
                    endpoint=self.endpoint,
                    model=self.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cached_input_tokens=cached_input_tokens,
                )
        except Exception:
            pass

        return resp.choices[0].message.content or ""


def _extract_json(text: str) -> str:
    """Pull the first JSON object out of a possibly-chatty response."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


# Local models (esp. Ollama/Mistral) often wrap the payload in a type name
# or `labels` envelope, or rename gate fields. Guided json_schema would
# reject that; json_object cannot prevent it.
_FIELD_ALIASES = {
    "topic_relevant": "is_topic_relevant",
    "ProductExtras": "extras",
    "product_extras": "extras",
}

# Wrapper keys that are never schema fields — strip null/empty siblings so a
# lone {"Classification": {...}, "Error": null} can unwrap.
_IGNORABLE_WRAPPER_VALUES = (None, "", [], {})


def _apply_field_aliases(
    data: dict[str, Any],
    fields: Optional[dict] = None,
) -> dict[str, Any]:
    out = dict(data)
    aliases = dict(_FIELD_ALIASES)
    # ``is_relevant`` is ambiguous: classify wants is_topic_relevant,
    # relevance wants ``relevant``. Prefer the field the schema actually has.
    if fields is not None:
        if "relevant" in fields and "is_topic_relevant" not in fields:
            aliases["is_relevant"] = "relevant"
            aliases["topic_relevant"] = "relevant"
            aliases["is_topic_relevant"] = "relevant"
        else:
            aliases["is_relevant"] = "is_topic_relevant"
    else:
        aliases["is_relevant"] = "is_topic_relevant"
    for old, new in aliases.items():
        if old in out and new not in out:
            out[new] = out.pop(old)
        elif old in out:
            out.pop(old, None)
    return out


def _strip_ignorable_keys(data: dict[str, Any], fields: dict) -> dict[str, Any]:
    """Drop null/empty non-schema keys so single-envelope unwrap can fire."""
    return {
        k: v for k, v in data.items()
        if k in fields or v not in _IGNORABLE_WRAPPER_VALUES
    }


def _flatten_structured(data: Any, schema_model: Type[BaseModel]) -> Any:
    """Unwrap nested envelopes so pydantic sees a flat schema-shaped dict.

    Handles shapes seen from Ollama classify failures, e.g.:
      {"Classification": {"is_topic_relevant": true, ...}}
      {"labels": {"is_topic_relevant": true}, "summary": "..."}
      {"topic_relevant": true, ...}
      {"Classification": {...}, "Error": null}
    """
    if not isinstance(data, dict):
        return data

    fields = schema_model.model_fields
    data = _apply_field_aliases(data, fields)
    data = _strip_ignorable_keys(data, fields)

    # Single non-schema key whose value is a dict → unwrap (Classification, …).
    if len(data) == 1:
        key, val = next(iter(data.items()))
        if key not in fields and isinstance(val, dict):
            return _flatten_structured(val, schema_model)

    # Merge wrapper dicts that carry schema fields (labels / nested models).
    wrappers = {
        k: v for k, v in data.items()
        if k not in fields and isinstance(v, dict)
    }
    if wrappers:
        nested_bits: dict[str, Any] = {}
        for v in wrappers.values():
            nested_bits.update(_apply_field_aliases(v, fields))
        if nested_bits.keys() & fields.keys():
            merged = dict(nested_bits)
            for k, v in data.items():
                if k in fields:
                    merged[k] = v
            return _flatten_structured(merged, schema_model)

    return data


def _fill_missing_required(data: Any, schema_model: Type[BaseModel]) -> Any:
    """Invent safe defaults when local models omit required gate fields.

    Empty ``{}`` and taxonomy dumps from Ollama/Mistral otherwise hard-fail
    after repair and either drop items or loop forever on resume.
    """
    if not isinstance(data, dict):
        return data
    fields = schema_model.model_fields
    data = _apply_field_aliases(dict(data), fields)
    # Drop non-schema keys (OperatingSystem, ProductExtras-as-list, …).
    out = {k: v for k, v in data.items() if k in fields}
    if "extras" in out and not isinstance(out["extras"], dict):
        out.pop("extras", None)

    # RelevanceResult — empty {} → not relevant at high confidence so the
    # drop threshold (default 0.7) actually removes the item. confidence=0
    # used to fail-open junk into classify.
    if "relevant" in fields and "relevant" not in out:
        out["relevant"] = False
    if "confidence" in fields and "confidence" not in out:
        out["confidence"] = 1.0 if out.get("relevant") is False else 0.0

    if "is_topic_relevant" in fields and "is_topic_relevant" not in out:
        out["is_topic_relevant"] = False
        if "summary" in fields and not str(out.get("summary") or "").strip():
            out["summary"] = "incomplete model JSON; marked not topic-relevant"
        if "areas" in fields and "areas" not in out:
            out["areas"] = []
        if "content_types" in fields and "content_types" not in out:
            out["content_types"] = []

    # Local models often emit areas as [{"type": "shell"}] and sentiment
    # outside [-1, 1]. Coerce before pydantic so repair isn't wasted.
    if "areas" in out:
        out["areas"] = _coerce_str_list(out["areas"])
    if "content_types" in out:
        out["content_types"] = _coerce_str_list(out["content_types"])
    if "sentiment" in out and isinstance(out["sentiment"], (int, float)):
        out["sentiment"] = max(-1.0, min(1.0, float(out["sentiment"])))
    return out


def _coerce_str_list(val: Any) -> list[str]:
    """Turn ``["a", {"type": "b"}]`` into ``["a", "b"]`` for list[str] fields."""
    if not isinstance(val, list):
        return []
    out: list[str] = []
    for item in val:
        if isinstance(item, str):
            s = item.strip()
            if s:
                out.append(s)
            continue
        if isinstance(item, dict):
            for k in ("id", "area", "name", "type", "value", "label"):
                v = item.get(k)
                if isinstance(v, str) and v.strip():
                    out.append(v.strip())
                    break
    return out


def _minimal_json_example(schema_model: Type[BaseModel]) -> str:
    """One-line example matching the schema the model must return."""
    fields = schema_model.model_fields
    if "relevant" in fields and "is_topic_relevant" not in fields:
        return '{"relevant": false, "confidence": 0.0}'
    if "is_topic_relevant" in fields:
        return (
            '{"is_topic_relevant": false, "areas": [], '
            '"content_types": [], "sentiment": 0, "summary": "", '
            '"extras": {}}'
        )
    # Generic fallback: list required field names.
    req = [n for n, f in fields.items() if f.is_required()]
    return "{" + ", ".join(f'"{n}": null' for n in req[:6]) + "}"


def _prepare_structured_json(text: str, schema_model: Type[BaseModel]) -> str:
    """Extract JSON and flatten local-model envelopes before validate."""
    extracted = _extract_json(text)
    try:
        data = json.loads(extracted)
    except json.JSONDecodeError:
        return extracted
    flat = _flatten_structured(data, schema_model)
    filled = _fill_missing_required(flat, schema_model)
    try:
        return json.dumps(filled)
    except (TypeError, ValueError):
        return extracted
