# Setup and onboarding

Get from zero to a first digest, then extend sources and LLM providers.

## Quick start

### Option A — published CLI

```bash
uvx product-monitor demo    # offline Notion demo, opens a report
uvx product-monitor ui      # http://127.0.0.1:8766
```

### Option B — source checkout

```bash
python -m venv .venv
# Windows: .venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -e .[dev]

product-monitor demo
product-monitor ui
```

### Option C — Docker

```bash
cp .env.example .env        # add keys when you need them
docker compose up           # UI on http://127.0.0.1:8766
```

Host mounts: `config/`, `products/`, `data/`, `reports/`, `.env`.
Code under `pipeline/`, `webui/`, `sources/` is baked into the image —
rebuild after Python changes:

```bash
docker compose build app && docker compose up -d app
```

One-off pipeline run:

```bash
docker compose run --rm app python -m pipeline.run --product <slug>
```

### Platform notes

- **Python 3.11+** on Windows, macOS, Linux.
- **macOS:** prefer Homebrew/pyenv Python — system `/usr/bin/python3` on
  older macOS can fail TLS to Reddit/Anthropic.
- **Linux:** digest v2’s embedding stage may need `libgomp1` (or use Docker).

## First product (wizard)

Open the UI → **Monitor your product** (wizard v2).

1. **Describe** — name, URL or short blurb, goals.
2. **Confirm profile** — review drafted description, aliases, scope,
   competitors, suggested sources; regenerate sections if needed.
3. **Sources** — pick **user feedback** (HN, Reddit RSS, Stack Exchange, …)
   and **media coverage** (catalog RSS publications). Configure stream
   identifiers when asked (subreddits, feed URLs, …). Calibrate on a
   small live sample (Relevant / Not relevant).
4. **Review** — taxonomy themes, schedule, pipeline LLM choice → create
   product and start the first run.

Secrets are written to `.env` only — never into YAML under `config/` or
`products/`.

## Connections and keys

| Surface | Purpose |
|---|---|
| `/connections` | Per-source and per-pipeline-LLM provider keys |
| `/wizard/llm` | Global **assistant** LLM (wizard, headlines, …) |
| Product → LLM routing | Which model/endpoint each pipeline stage uses |

Copy `.env.example` → `.env` for a full list of env var names.

### Keyless sources (work with no API key)

Hacker News, RSS / media catalog, Reddit RSS, Apple App Store, Mastodon,
Microsoft Tech Community, Discourse (optional key), Stack Exchange
(optional key for higher quota).

### Sources that need credentials

Examples: Reddit (OAuth), GitHub Issues/Discussions, YouTube Comments,
Product Hunt, Google Play (service account), Bluesky, ScrapeCreators
proxies. Configure on `/connections`, then enable streams on the product.

## Adding more sources

### Enable a built-in source for a product

1. Ensure the connection is ready on `/connections` (if it needs a key).
2. Product → **Sources** → enable the plugin and add streams
   (subreddit, `feed_url`, repo, package name, …).
3. Or re-run the wizard Sources step / pick from the media catalog for RSS.

Media coverage publications live in `config/media_sources.yaml` — add a
`name` + `feed_url` there to show up in the catalog.

### Author a new source plugin

1. Copy `plugins/example/` (or start from a built-in in `sources/`).
2. Define `MANIFEST = SourceManifest(...)` — `plugin_id`, display name,
   connection fields, stream fields, `content_types`
   (`user_feedback` / `media_coverage`).
3. Implement `fetch_since(...)` yielding `RawItem`s with attribution.
4. **Drop-in:** set `TRUST_PLUGINS_DIR=./plugins` (or `--trust-plugins-dir`).
5. **Packaged:** expose an entry point under `customer_feedback.sources`
   and `pip install` the package (no trust flag required).
6. Add a conformance/unit test; confirm the plugin appears on
   `/connections` and in the product Sources UI.

See `plugins/example/README.md` for the skeleton checklist.

## Adding / switching LLM providers

### Pipeline (per product)

1. Put the provider API key in `.env` (see `.env.example`).
2. Mark the provider ready on `/connections` (LLMs section).
3. On the product’s **LLM routing** page, pick provider + model per stage
   (relevance, classify, …). Local endpoints (Ollama, Foundry Local,
   vLLM, LM Studio, TabbyAPI) need a base URL, not a key.

### Assistant (global)

1. Open `/wizard/llm` (or Admin → Assistant LLM).
2. Choose Anthropic / OpenAI / Gemini / **Local (Ollama)** / …
3. Paste a key only for hosted providers — Ollama shows “no key needed”.
4. One env var, `ASSISTANT_LLM_API_KEY`, holds the current hosted key;
   switching hosted providers overwrites it. Local selection leaves any
   existing key alone for when you switch back.

### Supported provider families

**Hosted:** Anthropic, OpenAI, Google Gemini, Azure OpenAI, OpenRouter,
Groq, Together, Fireworks, DeepInfra, Perplexity, Mistral, Cohere.

**Local / OpenAI-compatible:** Ollama, Foundry Local, vLLM, LM Studio,
TabbyAPI — point the endpoint at localhost (or your internal gateway).

## Running and scheduling

```bash
product-monitor run --product <slug>
# optional: --skip-fetch, --source-ids rss,reddit_rss, resume flags, …
```

In the UI: **Runs** → trigger a run, watch the per-stream stage table,
open the digest when render/digest finishes. Docker Compose can also
start a headless scheduler profile — see `docker-compose.yml`.

## Where to look when something fails

| Symptom | Check |
|---|---|
| Source missing on pick list | Connection not ready / paused / needs key |
| RSS error `feed_url` missing | Re-select catalog publications on Sources |
| LLM stages skipped | Endpoint/key; Admin → Tokens / run log errors |
| Docker UI stale after code edit | `docker compose build && up -d` |
| Warehouse lock errors | Don’t hold DuckDB open in UI while a run writes |

Next: [Architecture](ARCHITECTURE.md) · [Key design decisions](DESIGN_DECISIONS.md)
