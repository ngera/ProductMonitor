# Customer Feedback Monitor

A cross-platform, open-source, locally-run tool that monitors any product or topic across multiple
feedback sources (Hacker News, RSS, Reddit, GitHub Issues, Stack Exchange, YouTube, more via
plugins), classifies items with a configurable LLM (hosted Anthropic / OpenAI / Gemini / Azure
OpenAI, or local Ollama / Foundry Local / any OpenAI-compatible endpoint), and generates static
HTML reports with full source attribution.

**License:** MIT — see [LICENSE](LICENSE).

## The 60-second version

```bash
# One-liner: full pipeline against a bundled Notion demo — no keys, no network,
# opens the report in your browser when done.
uvx feedback-monitor demo
```

Then, when you're ready to point it at your own product:

```bash
uvx feedback-monitor ui        # local admin webui at http://127.0.0.1:8765
```

The UI opens on a welcome page with two cards — "Try the demo" and "Monitor
your product." The second card is a guided wizard that pre-selects
credential-free sources and offers a three-button LLM chooser
(hosted API key / local Ollama / skip).

## Getting started (~5 minutes)

### 1. Run the offline demo

```bash
uvx feedback-monitor demo
# or, from a source checkout:
python -m pipeline.demo
```

You'll see the full pipeline run — normalize → filter → relevance → classify
→ group → score → aggregate → render — against a bundled capture of 15 real
Notion posts on Hacker News. LLM calls are served from a recorded response
bundle (ADR-0010) so the demo is deterministic and works with zero keys.

### 2. Monitor your own product

```bash
uvx feedback-monitor ui
```

Open http://127.0.0.1:8765. Wizard v2 (behind `wizard_v2_enabled`, see
[ADR-0014](documents/decisions/0014-wizard-v2-four-screen-flow.md)) is at
`/wizard`. Legacy v1 (behind `wizard_enabled`) is at
`/products/create/wizard`.

Wizard v2 is four screens — you supply facts, we draft the machinery:

1. **Describe** — product name, optional URL or short description, goal
   checkboxes.
2. **Confirm profile** — chips + cards for the LLM's drafted description,
   aliases, confusables, competitors, scope bullets, and suggested sources.
   Regenerate any section (up to 3 times), toggle keyed sources off if you
   don't want to set up their credentials.
3. **Calibrate** — a small live fetch across your enabled + keyless sources
   pops up a deck of ~10 real posts; you press Relevant / Not relevant /
   Skip. Judgments become seed snippets on the created product.
4. **Review & run** — monitor summary, source list, proposed areas grounded
   in the sampled posts, cost estimate, and a three-button LLM chooser
   (paste API key / Ollama / skip). "Create product & run" materializes
   the product and kicks off the first pipeline run.

You reach a running first report typing only: name, URL, goal checkboxes,
Y/N judgments, and (optionally) one API key.

## From a source checkout

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1      # macOS / Linux: source .venv/bin/activate
pip install -e .[dev]

feedback-monitor demo           # or: python -m pipeline.demo
feedback-monitor ui             # or: python -m webui.app
feedback-monitor run --product <slug>
```

`ensure_schema()` runs at the start of every pipeline invocation, so
there's no separate database init step. The old `scripts/init_db.py` is
kept for people who want the schema without the pipeline, but nothing in
the quickstart uses it.

## Configuration

- `config/app.yaml` — global pipeline knobs (filter thresholds, fetch limits,
  scoring, reporting). Also the fallback LLM routing when a product has none.
- `config/features.yaml` — feature flags (every post-V1 capability is off by
  default; see [ADR-0006](documents/decisions/0006-feature-flags-off-by-default.md)).
- `products/<id>/` — per-product config: taxonomy, vendors, sources, prompts,
  llm_routing, extras schema, seed snippets.
- `.env` — API keys (Reddit / OpenAI / Anthropic / GitHub / …). The webui's
  `/connections` page and the wizard's LLM step both write here in place.

## What the pipeline does

```
Fetch → Normalize → Filter → Relevance gate (LLM) → Classify+Extract (LLM)
      → Deterministic grouping → Score → Aggregate → Static HTML report
```

Raw JSONL is the system of record — every stage after fetch is idempotent
against the same raw data (`--skip-fetch` re-runs everything from raw).

If the LLM endpoint isn't reachable, the pipeline still runs Fetch /
Normalize / Filter and produces a partial report — the LLM stages are
skipped with a health-check note in the run log.

## Trusting your results

Golden-set labeling and F1 gates are worth the effort, but they aren't
required to get value out of the first few runs — they belong to the
"harden and trust" phase, not the "first look" phase. See
[documents/TRUSTING_YOUR_RESULTS.md](documents/TRUSTING_YOUR_RESULTS.md)
for the full recipe (`scripts/label_helper.py`, `eval/run_eval.py`, the
acceptance-gate flag, F1 targets, snippet split policy).

## Layout

| Path | Purpose |
|---|---|
| `config/` | Global YAML (app knobs, feature flags, model pricing) |
| `pipeline/` | Pipeline stages, orchestrator, CLI entry point |
| `sources/` | Built-in source connectors |
| `plugins/` | Drop-in third-party source plugins (opt in via `--trust-plugins-dir`) |
| `report_templates/` | Jinja2 templates for static HTML |
| `products/` | Per-product config (one folder per product) |
| `products/demo/` | Bundled Notion demo product |
| `data/demo/` | Bundled offline replay data for `feedback-monitor demo` |
| `eval/` | Golden-set eval harness (see [TRUSTING_YOUR_RESULTS.md](documents/TRUSTING_YOUR_RESULTS.md)) |
| `scripts/` | `init_db.py`, `label_helper.py`, `backup.py`, `build_demo_replay.py` |
| `data/<product>/` | Per-product raw JSONL, DuckDB warehouse, SQLite state, run logs |
| `reports/<product>/<week_id>/` | Generated HTML |

## Design docs

- [documents/first_run_solution.md](documents/first_run_solution.md) — the
  "minutes to first report" design this quickstart implements.
- [documents/POST_V1_PLAN.md](documents/POST_V1_PLAN.md) — post-V1 roadmap
  (plugin discovery, feature flags, evals, token attribution, replay adapter).
- [documents/DESIGN.md](documents/DESIGN.md) — pipeline design.
- [documents/decisions/](documents/decisions/) — architecture decision records.

## Tests

```bash
pytest
```
