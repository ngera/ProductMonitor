# ADR-0010: `replay://` LLM endpoint promotes the POST_V1 §4.12 mock adapter to a first-class scheme

- **Status:** Accepted
- **Date:** 2026-07-21
- **Area:** infra

## Context
The "minutes to first report" design ([first_run_solution.md §3](../first_run_solution.md))
requires an offline `feedback-monitor demo` command that runs the *entire*
real pipeline against a bundled capture — not a mock-up. Users need to
see relevance + classify + grouping + scoring + render exercised end-to-
end before deciding whether the tool is worth setting up.

Constraints:
- Zero keys, zero network at demo time.
- The demo bundle must fit under ~2 MB in a wheel.
- Prompt drift must fail loudly rather than silently serving stale output
  from an old bundle.
- CI tests already need a fake LLM (POST_V1 §4.12 planned a "mock adapter"
  for exactly this).

Doing this without touching `LLMClient` would require every stage that
uses an LLM to grow a "demo mode" branch, which is exactly the kind of
sprawl the plugin architecture was meant to prevent.

## Options considered

1. **Ship recorded prompts + responses as a JSONL bundle keyed by prompt
   hash; add a `replay://` scheme to `LLMClient` that looks them up and
   short-circuits the OpenAI client entirely.**
   - Pros: one code change; adapter doubles as CI test fixture; prompt
     changes automatically invalidate keys so stale bundles fail loudly.
   - Cons: bundle regen requires a small script (already needed anyway).

2. **Ship a JSON blob with pre-classified items; skip the LLM stages in
   demo mode.**
   - Pros: no `LLMClient` change.
   - Cons: demo doesn't exercise the actual pipeline; misleading first
     impression.

3. **Point `demo` at a hosted LLM with a free credit.**
   - Pros: real classification.
   - Cons: not offline, requires network + rate-limit handling, non-
     deterministic.

## Decision
Add `replay://` as a recognized `endpoint` scheme in `LLMClient`. When the
configured endpoint starts with that prefix, `LLMClient` skips the OpenAI
client construction, loads recorded responses from
`data/demo/llm_replay.jsonl` (or a caller-provided path), and returns them
keyed by `sha256(role, system, user)[:16]`.

Health check for a replay endpoint is `bundle_file_exists`.

Missing key → `LLMError("replay miss …")` with instructions to rebuild
the bundle. No silent degradation.

Companion script `scripts/build_demo_replay.py` regenerates the bundle
from a curated in-repo item list. Same script serves as the CI test
fixture generator.

## Rationale
- **One change, three payoffs**: `feedback-monitor demo` works offline,
  `pipeline.llm.LLMClient` gets a clean test seam without any mocking
  library, and the pipeline gets a deterministic end-to-end regression
  target.
- **Prompt drift is the only real risk** and the hash-key approach makes
  it fail loud — the alternative (fuzzy matching or content-based
  lookup) would let stale bundles serve wrong answers indefinitely.
- **Scheme namespacing** (`replay://…`) matches how the OpenAI SDK already
  routes on URL scheme; no new config surface.

## Consequences
- **Easy**: adding a CI test that runs the whole pipeline without a live
  LLM. Regenerating the demo bundle when prompts change. Third parties
  can build their own replay bundles (e.g., for their own docs).
- **Hard**: any prompt change to the demo product's `prompts.yaml`
  requires re-running `scripts/build_demo_replay.py` to regenerate keys;
  otherwise `feedback-monitor demo` breaks. This is the intended
  behavior (matches ADR-0007's structured-output-versioning ethos)
  but it means bundle regen is on the release checklist.
- **Locked in**: the key derivation (`sha256(role,system,user)[:16]`)
  is now a compatibility contract for anyone who ships their own
  replay bundle. Changes to `_prompt_key` would silently invalidate
  every bundle in the wild.

## Revisit if
- The demo bundle grows past ~10 MB (would want to switch to lazy /
  streaming lookup or move demo assets out of the wheel).
- We add multi-turn LLM interactions where a single (system, user)
  pair no longer captures the full conversation.
- We want fuzzy prompt matching (e.g., ignore trailing whitespace, few-
  shot ordering) — but note this weakens the drift-detection guarantee.
