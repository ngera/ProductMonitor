# ADR-0016: Persistent-issue stage clusters week_groups across time via local embeddings

- **Status:** Accepted
- **Date:** 2026-07-27
- **Area:** data / infra

## Context

The digest v2 design ([report_v2_design.md](../report_v2_design.md)) needs
cross-week issue identity: "Bug X has been mentioned 47 times across 8
weeks." Today [pipeline/group.py](../../pipeline/group.py) clusters items
only *within* a single week (`week_groups.week_id` is part of the primary
key). There is no stable identifier that says "this week's group and last
week's group are the same underlying issue."

Every digest section (Positive / Negative / Bugs / Features) surfaces
per-issue mention counts, priority rankings, and persistent-issue-scoped
detail pages. Without cross-week identity, every run of the digest would
show only current-week counts — which erases the signal the user cares
about most ("is this getting worse?"). The priority formulas in
[report_v2_design.md §10](../report_v2_design.md) bake in
`total_mentions_across_persistent_issue`; there is no useful fallback.

Constraints:
- Runs in every pipeline run without adding minutes to overall runtime.
- Deterministic run-over-run — rerunning the pipeline must not reshuffle
  persistent-issue-ids (breaks history counts).
- No network call per group. Pipeline runs offline are a supported mode
  ([ADR 0010](0010-replay-llm-endpoint.md)).
- Backfill is forward-only per the digest v2 design; historical
  week_groups keep no persistent-issue-id.

## Options considered

1. **Local sentence-transformers embeddings + cosine similarity match.**
   `all-MiniLM-L6-v2` (384-dim), threshold ~0.85. Match this run's week-
   group canonicals against embeddings from prior runs.
   - Pros: fully deterministic; runs offline; ms-per-group on CPU;
     catches paraphrases.
   - Cons: adds `sentence-transformers` + `torch` (~1 GB install);
     English-biased; embedding model becomes a compatibility contract.

2. **LLM adjudication.** For each new week's canonical, ask the assistant
   LLM whether it matches any of the last N weeks' canonicals.
   - Pros: highest fidelity for nuanced merges.
   - Cons: cost per new issue per run; non-deterministic run-over-run;
     requires network (breaks offline mode).

3. **Heuristic: canonical title + entity match.** Two groups match if
   their canonical titles share N normalized tokens AND they mention the
   same primary entity.
   - Pros: no ML, no dependency, offline-safe.
   - Cons: misses paraphrases ("audio stutter" vs "sound cutting out");
     over-merges when different issues share a keyword.

4. **Layered — heuristic first, LLM tie-break.**
   - Pros: cost-controlled quality.
   - Cons: two code paths to maintain; non-determinism from the LLM tier
     still creeps in on borderline pairs.

## Decision

**Option 1** — local sentence-transformers embeddings.

- Model: `all-MiniLM-L6-v2` (384-dim, ~90 MB weights).
- Match rule: cosine similarity > `digest.persistent_issue_threshold`
  (default `0.85`, tunable in [config/app.yaml](../../config/app.yaml)).
- Identity scope: **per product, per section** (Bugs / Features / Positive /
  Negative). A "bug that's also a feature request" gets one persistent-
  issue-id in the Bugs section and a separate one in the Features section.
- Storage: new tables (materialized in Slice 2):
  - `persistent_issues(product_id, section, issue_id, canonical_title,
    first_seen_week, last_seen_week, total_mentions, embedding_blob)`
  - `week_group_persistent_issue(week_id, section, group_key, issue_id)`
- Backfill: **forward-only.** Historical week_groups have no persistent-
  issue-id and produce no cross-week counts until re-processed. Wizard
  copy sets this expectation (per
  [report_v2_design.md §11](../report_v2_design.md)).

## Rationale

- **Determinism was the deciding constraint.** LLM adjudication (Option 2)
  would have shifted persistent-issue-ids across runs whenever the model
  changed its mind on a borderline pair. That would break every digest's
  history counts. Embeddings from a pinned model produce identical
  similarity scores across runs.
- **Offline-first is a hard requirement.** Options 2 and 4 both need
  the network for the LLM call. Local embeddings match `feedback-monitor
  demo`'s offline-first ethos ([ADR 0010](0010-replay-llm-endpoint.md)).
- **1 GB install cost is acceptable.** This is a workstation tool for
  product researchers, not a serverless function. The ~90 MB weights file
  and its runtime are a one-time install cost. The alternative cloud-
  embedding cost recurs every run and requires per-user API keys.
- **Section-scoped identity** (per-product-per-section, not global) is
  the pragmatic call. A complaint about Explorer crashing and a feature
  request for a stable Explorer embed to very different vectors. Section
  scoping matches how users read the digest.

## Consequences

**Easy:**
- Adding a new section (e.g. a future "Praise" section) — reuse the same
  stage with a different content-type filter.
- Tuning cluster tightness via one config knob without code changes.
- Backfilling on-demand later (post-v1) — the algorithm is idempotent.

**Hard / expensive:**
- **Dependency footprint.** `sentence-transformers` pulls in `torch`.
  This is the largest single dep addition in the project's history.
  First-run install time on a fresh venv goes from ~30 s to ~2 min.
- **Embedding model is now a compatibility contract.** Changing
  `digest.embedding_model` invalidates every persistent-issue-id in
  the database (embeddings from different models aren't comparable).
  Model swaps require a full recluster job.
- **English-biased.** Multi-language feedback (out of scope for v1 per
  the digest v2 non-goals) clusters worse than English. Post-v1 work
  would need a multilingual model.
- **`churn_signal` items** are surfaced within the Negative section's
  persistent-issue graph; churn does not have its own persistent-issue
  identity space. Fine for v1 (churn is a badge, not a section).

## Revisit if

- Install size becomes user-visible pain (users on constrained runners
  complain about the 1 GB weight file). Consider a smaller model
  (`all-MiniLM-L12-v2` — actually larger; `paraphrase-MiniLM-L3-v2` is
  smaller and faster but lower quality) or making the embedding backend
  pluggable.
- Multi-language products become important — switch to
  `paraphrase-multilingual-MiniLM-L12-v2` or a bigger multilingual model.
  Requires a full recluster.
- Cross-product identity is wanted (e.g. "Windows and Windows Server both
  have this KB5039212 bug"). Would require `product_id` to leave the
  identity key — a schema migration.
- LLM adjudication becomes cheap and deterministic enough
  (temperature=0 + guided-decoding) that its accuracy edge on
  paraphrases outweighs its residual non-determinism cost.
