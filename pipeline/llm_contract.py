"""LLM output contract (POST_V1_PLAN §4.15, ADR-0007).

Wraps `pipeline.llm.LLMClient` with:
- Pydantic response models — typed outputs everywhere.
- Provider-agnostic structured output (OpenAI-compat json_schema).
- Explicit retry-with-feedback semantics — capped at 2 attempts.
- Prompt-caching support via `LLMCallSpec.cacheable_system`. On Anthropic
  endpoints, the system prompt is emitted as an array-of-blocks with a
  `cache_control: {type: ephemeral}` marker (5-min TTL). On other
  providers, no-op — plain string content.

This module is the front door for all new LLM callers in Phase 2+
(wizard, snippet candidates, prompt suggestions, report rationale).
Existing pipeline stages (relevance, classify) can migrate gradually.

Usage:
    from pipeline.llm_contract import LLMResponseContract, LLMCallSpec
    from pydantic import BaseModel

    class MyResponse(BaseModel):
        relevant: bool
        confidence: float

    contract = LLMResponseContract(role="relevance")

    result: MyResponse = contract.call(LLMCallSpec(
        system="You classify Windows Media Platform posts.",
        user=f"Title: {title}\\nBody: {body}",
        response_model=MyResponse,
    ))
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional, Type, TypeVar

import structlog
from pydantic import BaseModel, ValidationError

from pipeline.llm import LLMClient, LLMError, _extract_json, cacheable_content

log = structlog.get_logger()

T = TypeVar("T", bound=BaseModel)


class LLMContractError(LLMError):
    """Raised when a call fails all retries. Includes the last error and
    the last raw response for diagnostics."""

    def __init__(self, message: str, *, last_error: str = "", last_raw: str = "") -> None:
        super().__init__(message)
        self.last_error = last_error
        self.last_raw = last_raw


@dataclass
class LLMCallSpec:
    """Everything a single LLM call needs. Groups params together so
    LLMResponseContract.call() has a stable signature."""

    system: str
    user: str
    response_model: Type[BaseModel]
    # Optional: how many retries after the initial call. Default 2.
    # Set to 0 for "one shot, no retries" (e.g., cheap deterministic checks).
    max_retries: int = 2
    # Optional: extra guidance appended to the system prompt if the
    # response fails validation. Overrides the default retry preamble.
    retry_hint: str = ""
    # Mark the system prompt as cacheable. On Anthropic endpoints, this
    # emits a cache_control ephemeral marker so the provider caches the
    # system prompt for ~5 min. No-op on other providers.
    cacheable_system: bool = False


class LLMResponseContract:
    """Typed LLM wrapper — the front door for all Phase 2+ LLM callers.

    One instance per LLM role (matches LLMClient's model). Reuses the
    per-product LLM routing configuration; the same endpoint + model as
    the equivalent LLMClient invocation.
    """

    def __init__(self, role: str) -> None:
        self._client = LLMClient(role)
        self.role = role
        self.model = self._client.model
        self.endpoint = self._client.endpoint

    def call(self, spec: LLMCallSpec) -> BaseModel:
        """Execute a typed LLM call. Returns a validated Pydantic instance
        of `spec.response_model`.

        Behavior:
        1. Build messages from spec.system + spec.user
        2. Call LLM with structured output (json_schema response_format)
        3. Parse + validate against response_model
        4. On ValidationError or JSONDecodeError: retry with structured
           feedback about what went wrong, up to spec.max_retries times
        5. Give up after retries; raise LLMContractError with diagnostics
        """
        system_content = (
            cacheable_content(spec.system, self.endpoint)
            if spec.cacheable_system
            else spec.system
        )
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": spec.user},
        ]

        last_error = ""
        last_raw = ""

        for attempt in range(spec.max_retries + 1):
            try:
                raw = self._client._call(messages, schema_model=spec.response_model)
            except Exception as e:
                # Endpoint doesn't support json_schema; fall back to plain JSON
                log.warning("llm_contract_structured_unsupported",
                           role=self.role, attempt=attempt, error=str(e))
                try:
                    raw = self._client._call(messages, schema_model=None)
                except Exception as e2:
                    last_error = str(e2)
                    if attempt == spec.max_retries:
                        raise LLMContractError(
                            f"LLM call failed on attempt {attempt + 1}: {e2}",
                            last_error=str(e2),
                            last_raw="",
                        ) from e2
                    continue

            last_raw = raw

            try:
                extracted = _extract_json(raw) if raw else raw
                return spec.response_model.model_validate_json(extracted)
            except (ValidationError, json.JSONDecodeError) as e:
                last_error = str(e)
                log.warning("llm_contract_validation_failed",
                           role=self.role, attempt=attempt, error=str(e))

                if attempt == spec.max_retries:
                    raise LLMContractError(
                        f"LLM response failed validation after "
                        f"{spec.max_retries + 1} attempts: {e}",
                        last_error=str(e),
                        last_raw=raw,
                    ) from e

                # Retry: send the model its own bad output + the parse error
                hint = spec.retry_hint or (
                    "Your previous response failed schema validation with:\n"
                    f"  {e}\n\n"
                    "Return ONLY corrected JSON matching the schema. No prose, no markdown."
                )
                messages = messages + [
                    {"role": "assistant", "content": raw or "(empty response)"},
                    {"role": "user", "content": hint},
                ]

        # Unreachable — the loop either returns or raises. Belt-and-braces.
        raise LLMContractError(
            "LLM contract exhausted retries without a decision",
            last_error=last_error,
            last_raw=last_raw,
        )
