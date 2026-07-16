"""LLM client wrapper for Foundry Local (DESIGN.md §4.4.1, §4.6, §10/§11.1).

Uses the `openai` SDK pointed at Foundry Local's OpenAI-compatible endpoint.
Prefers guided/JSON-schema-constrained decoding; falls back to a single
validate-and-repair call when it isn't available.
"""

from __future__ import annotations

import json
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


_LOCAL_HINTS = ("localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal")
_PROVIDER_ENV_HINTS = (
    # (substring of endpoint, env var)
    ("api.anthropic.com", "ANTHROPIC_API_KEY"),
    ("anthropic.com",     "ANTHROPIC_API_KEY"),
    ("api.openai.com",    "OPENAI_API_KEY"),
    ("openai.com",        "OPENAI_API_KEY"),
    ("openai.azure.com",  "AZURE_OPENAI_API_KEY"),
    ("googleapis.com",    "GOOGLE_API_KEY"),
    ("openrouter.ai",     "OPENROUTER_API_KEY"),
    ("groq.com",          "GROQ_API_KEY"),
    ("together.xyz",      "TOGETHER_API_KEY"),
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

        from openai import OpenAI

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
        api_key = _resolve_api_key(cfg, os.environ)
        self._client = OpenAI(
            base_url=cfg["endpoint"],
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
        try:
            self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ping"}],
                max_completion_tokens=1,
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
        """
        guided = self.cfg.get("use_guided_decoding", False) if guided is None else guided
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        raw_text: str = ""
        if guided:
            try:
                raw_text = self._call(messages, schema_model=schema_model)
                return schema_model.model_validate_json(raw_text)
            except (ValidationError, json.JSONDecodeError) as e:
                log.warning("guided_validation_failed", role=self.role, error=str(e))
            except Exception as e:
                # endpoint may not support json_schema response_format
                log.warning("guided_unsupported", role=self.role, error=str(e))

        # Free-form path
        if not raw_text:
            raw_text = self._call(messages, schema_model=None)
        try:
            return schema_model.model_validate_json(_extract_json(raw_text))
        except (ValidationError, json.JSONDecodeError) as e:
            return self._repair(messages, schema_model, raw_text, str(e))

    def _repair(
        self, messages: list[dict[str, str]], schema_model: Type[T], bad: str, err: str
    ) -> T:
        attempts = self.cfg.get("fallback_repair_attempts", 1)
        for _ in range(attempts):
            repair_msg = messages + [
                {"role": "assistant", "content": bad},
                {
                    "role": "user",
                    "content": (
                        f"Your previous output failed validation: {err}\n"
                        f"Return ONLY corrected JSON matching the schema. No prose."
                    ),
                },
            ]
            text = self._call(repair_msg, schema_model=None)
            try:
                return schema_model.model_validate_json(_extract_json(text))
            except (ValidationError, json.JSONDecodeError) as e:
                err = str(e)
                bad = text
        raise LLMError(f"{schema_model.__name__} validation failed after repair: {err}")

    def _call(
        self, messages: list[dict[str, str]], schema_model: Optional[Type[BaseModel]]
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.cfg.get("temperature", 0),
        }
        if self.cfg.get("seed") is not None:
            kwargs["seed"] = self.cfg["seed"]
        if schema_model is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_model.__name__,
                    "schema": schema_model.model_json_schema(),
                    "strict": True,
                },
            }
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
