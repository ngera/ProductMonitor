# ADR-0035: One press section, and empty LLM answers are failures

- **Status:** Accepted
- **Date:** 2026-11-11
- **Area:** data/frontend

## Context

A review of real digests on 2026-11-11 found three wrong outputs:

- **Supabase 2026-W39** listed a Verge article about Destiny 2 under Press
  coverage. llama3 (the product's pipeline model) answered relevance with
  confidence 0.0, which the borderline rule kept as relevant, then answered
  classify with `{"is_topic_relevant": true}` and nothing else. Only that
  field is required by the schema, so it counted as a successful
  classification. (The brand-mention pre-gate for media, added later that
  day, now blocks this article on its own.)
- **Cursor 2026-W40** listed a Reddit post under "Media Coverage — siloed
  from totals above". That section predates ADR-0028 and was keyed on the
  classifier's `news_discussion` tag, not the item's `content_type`. The
  post was also counted in the totals, so the label was false.
- Items whose classification failed were still counted as unique items, and
  failed items were retried on every re-run of a week with no limit.

ADR-0028 already decided how press appears: a top-of-digest Press coverage
block of `content_type='media_coverage'` items, items still in their area
sections, and totals covering every item once.

## Options considered

1. **Re-key the legacy section on `content_type` and exclude media from
   totals** — makes the "siloed" label true, but contradicts ADR-0028's
   totals rule and leaves two sections listing the same articles.
2. **Remove the legacy section; keep the ADR-0028 press block as the only
   press listing** — one place for press, totals as ADR-0028 defined them.
3. **Leave the sections, fix only the relevance and classify gates** — the
   Cursor post would still be mislabelled as media.

## Decision

Option 2, plus three gate changes:

- The legacy "Media Coverage (siloed)" section and
  `sections.media_coverage_items` are removed.
- Summary totals and the press block count only items with
  `filter_status='passed'` (a failed classification is not counted or shown).
- Relevance answers with confidence 0.0 are dropped (fail-closed, counter
  `zero_confidence`), like unparseable answers.
- A classification that says relevant but has no content types and no
  summary is a failure (`classification_failed`, counter `degenerate`).
- Failed classify calls are counted per item in `items.classify_attempts`
  (additive column); items stop being retried after
  `classify.max_attempts` (default 3) in `config/app.yaml`.

## Rationale

ADR-0028 is the accepted design for press, and option 1 would have needed
to supersede it for no reader benefit. The gate changes all apply one rule:
an answer the model could not commit to is not data. Showing it produced an
empty summary at the top of the Supabase digest, which costs more trust than
an item missing from it.

## Consequences

- Digests from weak local models will be shorter, sometimes nearly empty.
  That is the honest result and points at the model, not the digest.
- The industry-news split of media items is gone. With the brand-mention
  pre-gate for media it was almost always empty.
- `classify_attempts` survives re-normalize (`upsert_items` doesn't write
  it), so the cap holds across runs of the same week.

## Revisit if

- An eval set shows a model that legitimately answers relevance with
  confidence 0 for relevant items.
- Readers ask for every press item of the week, not the top
  `digest.press_coverage_top_n`: raise the default or add a detail page.
