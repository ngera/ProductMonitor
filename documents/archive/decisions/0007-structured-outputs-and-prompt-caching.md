# ADR-0007: Structured LLM outputs everywhere, prompt caching where supported

- **Status:** Accepted (implemented in `d907145`)
- **Date:** 2026-07-15 (accepted 2026-07-16)
- **Area:** infra
- **Plan reference:** [POST_V1_PLAN.md §4.15](../POST_V1_PLAN.md), D19, D20

## Context

V1's classify stage uses "please return JSON matching this schema"
prompts with `guided_unsupported` warning fallback when the endpoint
doesn't support guided decoding. Every LLM caller handles its own
retry, own parsing, own error path. Result: brittle, hard to test,
duplicated logic, occasional silent misclassification when JSON is
malformed.

Modern LLM APIs support structured outputs natively:
- **Anthropic:** `tool_use` — schema enforced server-side.
- **OpenAI (and OpenAI-compat):** `response_format: {type: "json_schema",
  schema: {...}}`.
- **Gemini:** function calling with response schema.
- **Local (Ollama, Foundry):** `response_format` (some), guided decoding
  (some), plain JSON parsing (fallback).

Post-V1 adds several new LLM use cases (wizard, snippet candidates,
prompt suggestions, rationale). Each has its own structured output
shape. Without a shared contract, we'd repeat the ad-hoc parsing
pattern N more times.

Separately: post-V1 makes many calls with the same prefix (system
prompt + few-shot examples + product context) and varying suffix
(per-item body). All three major providers now support prompt caching
with 50-90% cost reduction on cache hits. Anthropic in particular
provides `cache_control` for explicit ephemeral caches. Not using this
is money on the floor.

## Options considered

1. **Keep ad-hoc parsing.** Zero refactor. But: every new LLM use case
   duplicates the pattern; edge cases (missing fields, unknown keys,
   extra whitespace) trip the pipeline randomly.

2. **Instructor-style patterns.** Adopt the `instructor` library
   (Pydantic-first LLM output). Well-designed but adds a dependency.
   Supports major providers but tracks slowly on niche endpoints
   (Foundry, Ollama with unusual configs).

3. **In-house structured output contract.** New
   `pipeline/llm/contract.py:LLMResponseContract` wraps `LLMClient` +
   Pydantic response models. Auto-detects endpoint capability:
   Anthropic → tool_use; OpenAI-compat → json_schema; Gemini → function
   calling; local without support → guided decoding fallback → plain
   parsing. On failure: retry with structured error feedback ("your
   response was invalid because field X was missing"), cap at 2 retries.

4. **DSPy.** Full-blown LM programming framework. Overkill for this
   project's current scope.

## Decision

Option 3. In-house `LLMResponseContract`. Each LLM caller declares:

```python
class ClassifyResponse(BaseModel):
    primary_area: str
    content_types: list[str]
    sentiment: float
    # ...

response: ClassifyResponse = llm.call(
    prompt=...,
    response_model=ClassifyResponse,
)
```

Provider-specific structured mode picked automatically from endpoint.
Failure recovery centralized: parse errors trigger a retry with the
error included in the retry prompt. Cap at 2 retries; on 3rd failure,
raise and surface in `errors[]`.

Additionally: enable prompt caching on all providers that support it.
System prompt + few-shot block get `cache_control: {type: "ephemeral"}`
(Anthropic); OpenAI auto-caches repeated prefixes. Cache-hit token
counts recorded in `llm_usage.cached_input_tokens` (see ADR-0005).

## Rationale

- **Prevents a whole bug class.** "LLM returned malformed JSON" becomes
  a self-healing contract violation, not a runtime error.
- **Centralized retry logic.** One place to tune retry counts, error
  format, timeout — not scattered across 6+ callers.
- **Prompt caching is 5-10× cost saving** on typical few-shot pipelines
  where system + few-shot is 80% of every request. Free performance
  win.
- **No new dependency.** Pydantic is already in the codebase; provider
  SDKs already support structured modes. Instructor would be sugar over
  code we can write in a day.
- **Migration is one commit.** All existing LLM callers move to
  `LLMResponseContract` in one PR; behavior identical (or better —
  bad-JSON fallback is now retries, not warnings).

## Consequences

**Makes easy:**
- Adding a new LLM use case: define Pydantic model, one line to
  call. All parsing / retry / provider-specific plumbing is inherited.
- Testing LLM callers: mock the contract, deterministic responses.
- Cost reduction: caching hits are automatic.

**Makes hard or expensive to reverse:**
- Every LLM call now goes through the contract. Bypassing (e.g., for
  streaming) requires plumbing streaming through the contract.
  Deferred until streaming is a real requirement.
- Cache-hit token accounting depends on provider APIs exposing it.
  For providers that don't (some local models), we log `null` and
  the UI marks it "unknown."

**Locks us into:**
- Pydantic as the response schema language. Trivial to migrate to
  dataclasses if we ever need to (they'd share validation logic).
- Two-retry cap. Arbitrary but reasonable; tunable.

## Revisit if

- Streaming becomes important (agents that show intermediate output).
  Then the contract needs a streaming variant.
- A provider we depend on drops structured output support (unlikely;
  all major providers are moving toward more, not less).
- We adopt DSPy for a specific optimization use case. Contract stays
  as the runtime layer.
