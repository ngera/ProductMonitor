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


class LLMClient:
    """One client per LLM role ('relevance' | 'classify')."""

    def __init__(self, role: str) -> None:
        from openai import OpenAI

        cfg = app_config()["llm"][role]
        self.role = role
        self.cfg = cfg
        self.model = cfg["model"]
        self.endpoint = cfg["endpoint"]
        self._client = OpenAI(
            base_url=cfg["endpoint"],
            api_key="not-needed-for-local",
            timeout=cfg.get("timeout_seconds", 60),
            max_retries=cfg.get("max_retries", 3),
        )

    # --- health -------------------------------------------------------------

    def health_check(self) -> bool:
        """GET /v1/models before an LLM stage (§11.1)."""
        try:
            r = httpx.get(f"{self.endpoint}/models", timeout=10)
            return r.status_code == 200
        except Exception as e:
            log.warning("llm_health_check_failed", role=self.role, error=str(e))
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
