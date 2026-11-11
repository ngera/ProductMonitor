# ADR-0004: Golden set is a time-based split of the snippet pool

- **Status:** Accepted (implemented in `9690f70`)
- **Date:** 2026-07-15 (accepted 2026-07-16)
- **Area:** data
- **Plan reference:** [POST_V1_PLAN.md §4.10](../POST_V1_PLAN.md), D15
- **Supersedes:** none (revises D8 in v0.2 draft)

## Context

The Post-V1 plan needs a golden set for automated evals
([POST_V1_PLAN §4.10](../POST_V1_PLAN.md)). Snippets already exist as
few-shot examples for the LLM. It's tempting to reuse them as the
golden set — one data model, one UI, one workflow.

v0.2 of the plan proposed: snippets carry a `holdout_eval: bool` field;
users pick which snippets are training (few-shot) vs. holdout (eval).
Critical review flagged this as fatally flawed:

1. **Selection bias.** Users pick both training and holdout via the
   same UI at the same time. Users are more likely to add snippets
   that "look right" to them. The golden set becomes what the user
   *thinks* the LLM should output, not what's actually representative.
2. **Circular training/eval dependency.** LLM trains against snippets;
   evals score against snippets. Even with the holdout flag, both
   pools reflect the same user judgment. Passing evals means "the LLM
   agrees with the user" — not "the LLM is correct."
3. **Sample noise.** 5-20 holdout items produce noise-dominated metric
   estimates. Regression rules on point estimates will fire on random
   variation.

## Options considered

1. **User-picked holdout flag** (v0.2 proposal). Fails on selection
   bias and circular dependency.
2. **Time-based split.** Snippets carry `created_at`. Golden set =
   snippets created before a cutoff date. Training = after the cutoff.
   Cutoff auto-advances monthly. Removes user selection bias in
   holdout membership.
3. **Blind labeling.** Separate UI where the user labels items
   *without* seeing the LLM's prediction. Those labeled items become
   golden set only. Clean but expensive: user has to label items
   twice (once for snippets, once for golden set).
4. **External labeler.** Someone other than the user labels the
   golden set. Impractical for a personal tool.

## Decision

Option 2. Time-based split of the snippet pool. Per product:
`eval.golden_set_cutoff_date` in config. Snippets with `created_at`
before the cutoff are golden set; snippets with `created_at` at or
after the cutoff are training data (few-shot). Cutoff moves forward
automatically (default: monthly); manual override available.

Additional discipline:
- **Minimum 30 golden-set snippets** before evals produce numbers.
  Below that, UI shows "insufficient data — add more snippets" and
  no metric is computed. (Rationale: bootstrap CI on <30 items is
  dominated by noise.)
- **All eval metrics reported with 95% bootstrap CIs**, not point
  estimates.
- **Regression rule:** metric drop of >5pp AND 95% CI on the delta
  excludes zero. Both conditions required.

## Rationale

- **No selection bias in holdout membership.** The user doesn't
  choose which snippets go to the golden set — time does.
- **Approximates real deployment.** "Does the model classify content
  it wasn't recently trained on?" is the question that matters.
  Time-based split answers exactly that.
- **Circular dependency reduced but not eliminated.** Old snippets in
  the golden set are still user-authored. But the user authored them
  in a past mental context, so LLM decisions today have to generalize
  to what the user believed *then*. This is meaningfully different
  from "does the LLM agree with the user right now."
- **Sample sizes forced explicit.** Bootstrap CIs prevent false
  regression alarms; 30-item minimum prevents shipping meaningless
  numbers.

## Consequences

**Makes easy:**
- No UI for holdout selection. Users don't have to think about it.
- Cutoff advancement is automatic and predictable.
- Golden set grows organically as users add snippets and time passes.
- Metric CIs make it clear when a "regression" is real vs. noise.

**Makes hard or expensive to reverse:**
- Existing snippets need `created_at` backfill (from file mtime).
  Migration script required (see
  [POST_V1_PLAN §4.13](../POST_V1_PLAN.md)).
- New products have empty golden sets for months. Mitigation:
  wizard step 7 seeds a few snippets dated pre-cutoff so evals can
  start immediately.
- Users who WANT to force a specific snippet into the golden set can't
  do so directly — they'd have to date it manually or wait. This is a
  feature, not a bug; the whole point is to prevent user cherry-picking.

**Locks us into:**
- Snippets carrying `created_at`. Already implicit (file mtime).
- Monthly cutoff advancement rhythm. Arbitrary but reasonable.

## Revisit if

- Users routinely want to manually pick specific golden-set items
  (evidence: repeated asks). Reconsider a hybrid: time-based default
  with manual pin capability.
- 30-item minimum locks small-scale users out of evals for months.
  Reconsider a smaller floor with wider CIs and stronger "unreliable"
  warnings.
- Selection bias reappears via *snippet addition patterns* (users only
  add snippets that support their model's current output). This is a
  subtler failure mode; would require analyzing snippet class
  distribution over time.
