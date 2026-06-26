# Customer Feedback Monitor — V1

A locally-run tool that gathers Windows-related user feedback from Reddit, classifies it with a
local LLM (Phi-4-mini via Foundry Local), groups items into issues using deterministic keys, and
generates static weekly HTML reports with full source attribution.

See [documents/DESIGN.md](documents/DESIGN.md) for the full design. This README covers V1 only.

## What V1 does

```
Fetch (Reddit) → Normalize → Filter → Relevance gate (LLM) → Classify+Extract (LLM)
              → Deterministic grouping → Score → Aggregate → Static HTML report
```

No web UI, no embeddings, no clustering, Reddit-only. Config is hand-edited YAML.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Reddit credentials
copy .env.example .env   # then edit .env

# Initialize the warehouse + state DBs
python scripts\init_db.py
```

### Foundry Local

Foundry Local is a **prerequisite**, managed outside this project. It must expose an
OpenAI-compatible endpoint (default `http://localhost:5273/v1`) serving `phi-4-mini`.
Configure the endpoint/model in `config/app.yaml`.

If Foundry Local is not reachable, the pipeline still runs Fetch/Normalize/Filter and writes raw
data; the LLM stages will fail fast with a clear health-check error.

## Run

```powershell
# Full weekly pipeline
python -m pipeline.run --week 2026-W22

# Or via the batch wrapper
run_weekly.bat
```

Reports land in `reports/<week_id>/index.html` — open directly in a browser.

## Eval first

Per the design, build the golden set and confirm model quality **before** trusting output:

```powershell
# Label a Reddit URL into the golden set
python scripts\label_helper.py https://reddit.com/r/Windows11/comments/...

# Run the eval harness
python eval\run_eval.py
```

Acceptance gates (see DESIGN.md §7.3): F1 ≥ 0.75 on areas/content_types/severity/entities/KB,
sentiment 3-class accuracy ≥ 0.70, primary-area accuracy ≥ 0.80.

## Layout

| Path | Purpose |
|---|---|
| `config/` | Hand-edited YAML (sources, taxonomy, vendors, app) |
| `pipeline/` | Pipeline stages + orchestrator (`run.py`) |
| `sources/` | Pluggable source connectors (`reddit.py`) |
| `report_templates/` | Jinja2 templates for static HTML |
| `eval/` | Golden set + eval harness |
| `scripts/` | `init_db.py`, `label_helper.py`, `backup.py` |
| `data/` | Raw JSONL (system of record), DuckDB warehouse, SQLite state |
| `reports/` | Generated HTML, one folder per week |

## Tests

```powershell
pytest
```
