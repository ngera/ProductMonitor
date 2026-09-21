# Architecture

ProductMonitor is a **local-first, single-tenant** pipeline that watches
public feedback and media coverage about a product, classifies what
matters with a configurable LLM, and writes **static HTML digests** you
open in a browser. Nothing is multi-tenant. There is no shared warehouse
and no phone-home — each product’s data stays under `data/<product_id>/`
on the machine that runs it.

## What runs where

```
┌─────────────────┐     ┌──────────────────────────────┐
│  Web UI         │     │  Pipeline (CLI / UI / Docker) │
│  127.0.0.1:8766 │────▶│  fetch → … → digest          │
└─────────────────┘     └──────────────────────────────┘
         │                            │
         │ reads/writes               │ writes
         ▼                            ▼
   config/  products/  .env     data/<product>/  reports/<product>/
```

- **Web UI** — product wizard, connections, runs, taxonomy, prompts.
  Binds to `127.0.0.1` by default.
- **Pipeline** — one run over a time window (usually a week). Triggered
  from the UI, CLI (`product-monitor run`), or optional scheduler.
- **Config** — global knobs in `config/`; per-product silos in
  `products/<id>/`; secrets only in `.env`.

## Pipeline stages

Order is fixed. Stages after fetch are idempotent against raw JSONL
(`--skip-fetch` reprocesses without hitting the network again).

| Stage | Role |
|---|---|
| **fetch** | Pull items from enabled source plugins into raw JSONL |
| **normalize** | Canonical shape + cross-source URL dedup |
| **filter** | Cheap deterministic gates (length, language, …) |
| **relevance** | LLM (or heuristic) gate: is this about *this* product? |
| **classify** | LLM structured labels (themes, sentiment, …) |
| **score** | Rank items for the digest |
| **group** | Cluster related items for the week |
| **persistent_issue** | Optional: link groups across weeks (embeddings) |
| **aggregate** | Rollups for charts and section headers |
| **eval** | Optional golden-set metrics |
| **render** | Legacy HTML paths (when digest v2 is off) |
| **digest** | Digest v2 — primary static report artifact |

If the LLM endpoint is down, fetch / normalize / filter still complete
and the run records which LLM stages were skipped.

## Data layout (per product)

| Path | Contents |
|---|---|
| `products/<id>/` | `product.yaml`, `sources.yaml`, `taxonomy.yaml`, `prompts.yaml`, `llm_routing.yaml`, snippets |
| `data/<id>/raw/<source>/<week>/` | Raw JSONL — system of record for reprocessing |
| `data/<id>/warehouse.duckdb` | Queryable warehouse (items, usage, headlines, …) |
| `data/<id>/run_logs/` | Per-run JSON + stdout |
| `data/<id>/temp_runs/<run_id>/` | Stage snapshots for the run detail UI |
| `reports/<id>/<week_id>/` | Static HTML digest |

Week ids are ISO `YYYY-Www`. Timestamps in storage are UTC.

## Sources as plugins

Every fetch source is a plugin with a **manifest** (connection fields,
stream fields, content types) and a `fetch_since()` implementation.

Three discovery paths share one schema:

1. **Built-in** — `sources/*.py` shipped with the app
2. **Drop-in** — `plugins/*.py` or `plugins/<name>/plugin.py` (only when
   `TRUST_PLUGINS_DIR` / `--trust-plugins-dir` is set)
3. **Entry points** — `pip install` packages declaring
   `customer_feedback.sources`

Content is tagged as **user feedback** and/or **media coverage** so the
UI and digest can separate community posts from press/RSS.

## Two LLM roles

| Role | Config | Used for |
|---|---|---|
| **Pipeline LLM** | `products/<id>/llm_routing.yaml` | Relevance, classify, and other stage calls — pick model per stage |
| **Assistant LLM** | `config/assistant_llm.yaml` + `ASSISTANT_LLM_API_KEY` | Wizard drafting, taxonomy proposals, headlines, cosmetic passes |

You can run the pipeline on a cheap/local model and keep a stronger
hosted model for the assistant (or the reverse). Local providers
(Ollama, Foundry Local, LM Studio, …) need no API key.

## Reports

With digest v2 enabled, the sole operator-facing artifact is the weekly
**digest**: thematic sections, attributed quotes, optional charts and
persistent-issue pages. Attribution is mandatory — every displayed item
includes source provenance.

## Non-goals (by design)

- No multi-tenancy or per-user auth in the default product
- No cross-product intelligence warehouse
- No default-on outbound integrations (webhooks are opt-in)
- No real-time / streaming pipeline — the unit of work is a scheduled run

See also [Setup](SETUP.md) and [Key design decisions](DESIGN_DECISIONS.md).
