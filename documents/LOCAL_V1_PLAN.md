# Local-Run V1 Plan — Customer Feedback Monitor

**Version:** 0.1
**Status:** Active — this is the current top-level plan
**Supersedes:** [COMMERCIAL_MVP_PLAN.md](COMMERCIAL_MVP_PLAN.md) (cloud SaaS direction is now deferred indefinitely)
**Sibling docs:** [PLATFORM_DESIGN.md](PLATFORM_DESIGN.md) (long-term architecture, scoped down for V1), [DESIGN.md](DESIGN.md) (Windows reference deployment), [REDDIT_APPROVAL_PLAN.md](REDDIT_APPROVAL_PLAN.md) (each user registers their own personal Reddit app)

## 1. What V1 is

A **cross-platform, open-source, local-run** tool that lets a user monitor any product or topic across multiple feedback sources, classify items with a configurable LLM, and render a static HTML report.

- **Local-run.** Runs on the user's own machine (Windows, macOS, Linux). No cloud. No SaaS. No accounts.
- **Multi-topic.** Configure one or more topics in YAML; run any of them with a CLI flag. Windows is the reference topic; users can add their own (a product line, a competitor, a hobby community).
- **Open-source, MIT-licensed.** Code on a public GitHub repo, anyone can use, fork, embed.
- **Plugin-based.** Sources and LLMs are loaded via Python entry points. Adding a new source or LLM provider is writing one plugin against a stable contract.
- **Configurable LLM provider.** Not locked to any one LLM. Adapters for: hosted (Anthropic Claude, OpenAI, Gemini, Azure OpenAI) via the user's own API key; local (Foundry Local, Ollama, llama.cpp server, any OpenAI-compatible HTTP endpoint).

## 2. Scope

### In scope for V1

| Capability | Notes |
|---|---|
| **Topic-agnostic core data model** | `CoreClassification` (generic) + per-topic `extras` schema. Windows fields become a `WindowsExtras` plugin. |
| **Plugin contracts** | `Source` ABC + `SourceManifest`; `LLMAdapter` ABC + `LLMAdapterManifest`. Loaded via `importlib.metadata` entry points. |
| **Pipeline stages** | Fetch → Normalize → Filter → Relevance → Classify+Extract → Group → Score → Aggregate → Render. Same shape as [DESIGN.md §4](DESIGN.md). |
| **Sources, day-one** | Reddit (non-commercial), Hacker News (Algolia), GitHub Issues (PAT). |
| **LLM adapters, day-one** | Anthropic Claude (hosted), OpenAI (hosted), Ollama (local cross-platform), Foundry Local (local Windows-only), generic OpenAI-compatible HTTP. |
| **Storage** | DuckDB (warehouse) + JSONL (raw) + SQLite (state). Local files only, per [DESIGN.md §5](DESIGN.md). |
| **Reports** | Static HTML to `reports/<topic_id>/<week_id>/`. |
| **Sample snippets** | YAML-stored per topic (no UI yet); used as few-shot examples in LLM prompts and as eval gold. |
| **CLI** | `feedback-monitor run --topic <id>`, `feedback-monitor topics list`, `feedback-monitor eval --topic <id>`. |
| **Distribution** | GitHub repo, `pip install -e .` for now; PyPI later. |
| **License** | MIT. |

### Out of V1

Defer to V2+. All listed in [PLATFORM_DESIGN.md §11](PLATFORM_DESIGN.md) phase order but the local-run posture reshuffles priorities:

- Admin web UI (V1.5 — minimal local-only FastAPI + HTMX, bound to 127.0.0.1)
- REST API / service mode ([PLATFORM_DESIGN.md §8](PLATFORM_DESIGN.md)) — never, unless someone forks for SaaS
- Multi-tenancy — never in this codebase
- Auth, RBAC, billing — never
- Cloud hosting / managed deployment — never
- Reddit commercial agreement — never; each user uses their own non-commercial Reddit credentials
- Compliance certifications (SOC 2, GDPR DPAs, etc.) — out of scope for a local tool; each user is responsible for their own data
- Embedding-based clustering (V2 in [DESIGN.md §3](DESIGN.md))
- Cross-week issue identity (V1.5 in [DESIGN.md §3](DESIGN.md))

## 3. Architecture (V1 cut)

The architecture from [PLATFORM_DESIGN.md §3](PLATFORM_DESIGN.md), trimmed to the local-run subset:

```
┌────────────────────────────────────────────────────────────────────┐
│   CLI: feedback-monitor run --topic <id>                           │
└────────────────────────────────────────────────────────────────────┘
                                │
┌────────────────────────────────────────────────────────────────────┐
│   Core Pipeline Orchestrator                                       │
│   Fetch → Normalize → Filter → Relevance → Classify → Group        │
│         → Score → Aggregate → Render                               │
└──────┬──────────────────────────┬──────────────────────────────────┘
       │                          │
┌──────▼──────┐         ┌─────────▼────────┐
│  Sources    │         │   LLM Router     │
│  Registry   │         │  (capability-    │
│             │         │   tagged)        │
│ reddit      │         │                  │
│ hn          │         │ anthropic        │
│ github      │         │ openai           │
│             │         │ ollama           │
│ (more in    │         │ foundry_local    │
│  V1.5+)     │         │ openai_compat    │
└─────────────┘         └──────────────────┘
       │                          │
┌──────▼──────────────────────────▼──────────────────────────────────┐
│   Local Storage                                                    │
│   data/raw/<topic_id>/<source>/<week_id>/items.jsonl               │
│   data/warehouse.duckdb                                            │
│   data/state.sqlite                                                │
│   reports/<topic_id>/<week_id>/*.html                              │
└────────────────────────────────────────────────────────────────────┘
```

Nothing networked except the source fetches and (optionally) hosted LLM calls. No background workers, no schedulers, no queues — runs are CLI-triggered. A user's OS scheduler (cron, Task Scheduler) handles cadence.

## 4. LLM providers

V1 ships adapters for the providers below. The router picks one per stage based on the topic's config; users bring their own API keys (stored in `.env`, never committed).

| Adapter | Type | Stages | Notes |
|---|---|---|---|
| `anthropic` | Hosted | `relevance`, `classify` | Strict JSON schema via tool use. Recommended default for users with an API key. Sonnet for classify, Haiku for relevance. |
| `openai` | Hosted | `relevance`, `classify` | Strict structured output (`response_format=json_schema`). GPT-4o-mini for relevance, GPT-4o or GPT-4.1 for classify. |
| `gemini` | Hosted | `relevance`, `classify` | Schema mode supported. Flash for relevance, Pro for classify. |
| `azure_openai` | Hosted | `relevance`, `classify` | Identical to `openai` adapter pointed at an Azure endpoint. For users with Azure subscriptions. |
| `ollama` | Local | `relevance`, `classify` | Cross-platform (Mac/Linux/Windows). Easy install. Default for users who want fully-offline. Model selection in topic config. |
| `foundry_local` | Local (Windows) | `relevance`, `classify` | Windows-only. Already in current code. Kept for users on Windows with GPU and existing Foundry Local install. |
| `openai_compat` | Local or hosted | `relevance`, `classify` | Generic OpenAI-compatible HTTP. Covers llama.cpp server, vLLM, LM Studio, LocalAI, Together, Groq, Fireworks, OpenRouter. User points it at any URL. |

A user with no LLM at all gets a `pip install feedback-monitor[anthropic]` quick-start path in the README — install the extra, drop `ANTHROPIC_API_KEY` in `.env`, run.

The `embed` capability (for V2's clustering) is deliberately not in V1's adapter set yet — embed adapters come when clustering does.

### LLM cost expectations for a user

Rough costs per weekly run, ~300 items classified through relevance + classify:

| Adapter | Cost / week | Notes |
|---|---|---|
| Anthropic (Haiku relevance + Sonnet classify) | ~$0.10–0.30 | Predictable; quality is high |
| OpenAI (gpt-4o-mini relevance + gpt-4o classify) | ~$0.05–0.20 | Cheapest hosted |
| Gemini (Flash + Pro) | ~$0.05–0.15 | Cheapest hosted |
| Local (Ollama / Foundry Local) | $0 | Variable quality; depends on model |

A user can run a year on hosted LLMs for less than a coffee budget. This was not the case for the original Phi-4-mini-only design — V1 should not force local-LLM friction on users who'd happily pay $5/year.

## 5. Sources for V1

Three to ship with. Each gets a small `documents/SOURCE_<name>.md` written when its plugin lands.

| Source | Auth | Approval | Status |
|---|---|---|---|
| **Reddit** (non-commercial) | Personal Reddit Script app + non-commercial Data API approval | 2–4 weeks | See [REDDIT_APPROVAL_PLAN.md](REDDIT_APPROVAL_PLAN.md) — each user follows it themselves |
| **Hacker News** (Algolia) | None | None | Ship immediately |
| **GitHub Issues** | Personal access token (PAT) | None | Ship immediately |

V1.5+ candidates (not blocking V1): Microsoft Tech Community RSS, Stack Exchange, generic RSS, YouTube comments, Bluesky, Mastodon. See [PLATFORM_DESIGN.md §10](PLATFORM_DESIGN.md) for the full menu.

Reddit is the only V1 source with an approval wait — users without Reddit credentials can still use the tool with HN + GitHub from day one.

## 6. Distribution, installation, license

### License

**MIT.** Permissive, contributor-friendly, embeddable in commercial products. License file at repo root.

### Installation

V1:
```bash
git clone https://github.com/<owner>/feedback-monitor
cd feedback-monitor
python -m venv .venv
.venv\Scripts\activate           # Windows
# source .venv/bin/activate      # macOS / Linux
pip install -e ".[anthropic]"    # or [openai], [ollama], [foundry-local], [all]
cp .env.example .env             # edit with credentials
feedback-monitor topics init windows-11   # scaffolds a topic
feedback-monitor run --topic windows-11
```

V1.5+: published to PyPI; `pipx install feedback-monitor`.

### Packaging structure

The current flat layout (`pipeline/`, `sources/`, `eval/`, `scripts/`) becomes a proper package:

```
feedback_monitor/                # package root
├── core/                        # orchestrator, contracts, storage, models
├── plugins/                     # in-tree first-party plugins
│   ├── sources/
│   └── llms/
├── topics/                      # in-tree topic configs (windows is the reference)
└── cli/                         # feedback-monitor entry point
pyproject.toml                   # entry points, extras, build config
```

`pyproject.toml` declares optional extras (`anthropic`, `openai`, `ollama`, `foundry-local`, `all`) so users only install what they use.

### Open-source posture

- `LICENSE` — MIT
- `README.md` — quickstart + topic config example + LLM provider matrix
- `CONTRIBUTING.md` — how to add a source plugin, how to add an LLM adapter, dev setup
- `CODE_OF_CONDUCT.md` — Contributor Covenant
- `.github/` — issue templates (bug, feature, new source request), PR template, GitHub Actions CI (lint + tests)

## 7. Build phases

Each phase is independently usable. The existing Windows monitor (current code) keeps working at each phase boundary.

### Phase 0 — Plugin contracts & package layout (~1 week)

- Define `Source` ABC + `SourceManifest` and `LLMAdapter` ABC + `LLMAdapterManifest` in `core/contracts/`.
- Refactor existing [sources/reddit.py](../sources/reddit.py) into `plugins/sources/reddit/`.
- Refactor [pipeline/llm.py](../pipeline/llm.py) Foundry Local code into `plugins/llms/foundry_local/`.
- Split `Classification` ([pipeline/models.py:99-129](../pipeline/models.py#L99-L129)): keep generic fields in `CoreClassification`, move Windows-specific fields into `plugins/topics/windows/extras.py`.
- Restructure into proper package; add `pyproject.toml` with entry points.
- **Exit criterion:** existing weekly Windows run produces an identical report after the refactor.

### Phase 1 — Hosted LLM adapters (~1 week)

- Anthropic adapter (`plugins/llms/anthropic/`). Strict JSON schema via tool use.
- OpenAI adapter (`plugins/llms/openai/`). Strict structured output.
- LLM router with per-stage routing config (`relevance` and `classify` independently).
- `openai_compat` adapter for generic OAI-HTTP endpoints (covers vLLM, LM Studio, OpenRouter, etc. and as a bonus covers Foundry Local more cleanly than the current direct integration).
- **Exit criterion:** Windows topic runs end-to-end on Anthropic with no Foundry Local dependency.

### Phase 2 — Ollama adapter + cross-platform validation (~1 week)

- Ollama adapter (`plugins/llms/ollama/`). Cross-platform native.
- Test the full pipeline on macOS and Linux (your dev box is Windows; need at least a VM or container for Linux validation).
- Document Ollama quickstart in README.
- **Exit criterion:** documented "Ollama on Mac" path works end-to-end on a clean install.

### Phase 3 — Hacker News source plugin (~3 days)

- HN Algolia plugin (`plugins/sources/hn/`).
- Topic-agnostic config: per-topic search queries fed to HN search.
- **Exit criterion:** running a topic with `hn` configured produces classified HN items in the report.

### Phase 4 — GitHub Issues source plugin (~3 days)

- GitHub Issues plugin (`plugins/sources/github_issues/`) using a personal PAT.
- Topic config: list of `repo:` and optional `include_labels:` / `exclude_labels:`.
- **Exit criterion:** running a topic with `github_issues` configured produces classified GH issues in the report.

### Phase 5 — Sample snippets (YAML, no UI yet) (~3 days)

- `topics/<id>/examples/positive/*.yaml` and `examples/negative/*.yaml` files.
- Loader in `core/topic.py` reads them into a `TopicExamples` object.
- Few-shot injection into relevance + classify prompts (last 3 of each polarity).
- Held-out 30% reserved for the eval harness automatically by file path or a `holdout: true` flag.
- `feedback-monitor examples add --topic <id> --url <reddit-url>` CLI: fetches the raw item, dumps a YAML template the user edits.
- **Exit criterion:** seeding 10 snippets noticeably moves the eval harness metrics.

### Phase 6 — Polish for first public release (~1 week)

- README rewrite for an outside audience.
- CONTRIBUTING.md with the source/LLM plugin contracts documented in plain English.
- LICENSE, CODE_OF_CONDUCT.md.
- GitHub Actions CI: ruff lint, mypy, pytest on Python 3.11/3.12, all three OS.
- `feedback-monitor topics init <id>` CLI scaffolds a new topic directory with sensible defaults.
- `pip install -e .` works on a clean clone.
- **Exit criterion:** a stranger could clone, follow the README, and produce their first report inside 30 minutes (assuming they already have one source's credentials).

### Total: ~6 weeks of one-engineer time

Compared to:
- Personal Windows POC (original [V1_PLAN.md](V1_PLAN.md)): 6 days
- Commercial SaaS MVP (now-obsolete [COMMERCIAL_MVP_PLAN.md](COMMERCIAL_MVP_PLAN.md)): 8 weeks

V1 sits between — more ambitious than the personal POC (it's a *platform*), less ambitious than the SaaS (no cloud, no multi-tenant, no auth, no billing).

## 8. Migration map from current code

Same shape as [PLATFORM_DESIGN.md §13](PLATFORM_DESIGN.md) but with cloud/multi-tenant items removed:

| Current | Target |
|---|---|
| [sources/reddit.py](../sources/reddit.py) | `plugins/sources/reddit/source.py` + `manifest.py` |
| [sources/base.py](../sources/base.py) | `core/contracts/source.py` (extended with capabilities + manifest) |
| [pipeline/llm.py](../pipeline/llm.py) | `plugins/llms/foundry_local/adapter.py` + `core/router/llm_router.py` |
| [pipeline/run.py](../pipeline/run.py) | `core/orchestrator/run.py` + CLI wrapper in `cli/main.py` |
| [pipeline/models.py](../pipeline/models.py) `Classification.windows_*` | `plugins/topics/windows/extras.py` |
| [config/sources.yaml](../config/sources.yaml) | `topics/windows/topic.yaml` |
| [config/taxonomy.yaml](../config/taxonomy.yaml) | `topics/windows/taxonomy.yaml` |
| [config/vendors.yaml](../config/vendors.yaml) | `topics/windows/vendors.yaml` |
| [config/app.yaml](../config/app.yaml) | Split: per-topic LLM routing → `topics/<id>/llm_routing.yaml`; global app paths → `~/.feedback-monitor/config.yaml` |
| [eval/](../eval/) | `core/eval/` + `topics/<id>/examples/` |

Each move is a `git mv` then small edits to import paths.

## 9. What's deferred to V1.5 / V2

In order of when they likely matter:

1. **Local-only admin web UI** (V1.5). FastAPI + HTMX bound to 127.0.0.1. Topic CRUD, source CRUD, sample-snippet labeling form, report viewer. Optional — CLI users skip it.
2. **More source plugins** (V1.5 → V2). Microsoft Tech Community, Stack Exchange, generic RSS, YouTube comments, Bluesky, Mastodon.
3. **Embedding-based clustering** (V2). Catches duplicate issues that deterministic grouping misses.
4. **Cross-week issue identity + resurface tracking** (V1.5 of the old design).
5. **Enrichment pass** with a web-search LLM (Perplexity / Sonar / Tavily / Exa) per issue.
6. **Active-learning loop**: items the LLM classified at low confidence get surfaced in the snippet labeler for the user to correct.
7. **PyPI release** with `pipx install feedback-monitor` quickstart.
8. **Docker image** for users who'd rather run it as a container.

Service-mode, multi-tenant, cloud hosting, REST API, auth, billing — **never in this codebase.** If those become a real product later, they'd live in a separate fork; the plugin contracts and core models would carry over but the deployment shape would be different. [PLATFORM_DESIGN.md](PLATFORM_DESIGN.md) §8 (service mode), §4.1 (multi-tenant data isolation), §6.2 (PII deletion propagation) are kept as reference for that hypothetical fork — not as roadmap items for this project.

## 10. Open questions

Small, non-blocking. Decide as we get to them.

1. **GitHub org / repo name** — needs to exist before any public push. Suggestion: `feedback-monitor` under a personal handle or a dedicated GitHub org.
2. **Python minimum version** — propose 3.11 (matches current [requirements.txt](../requirements.txt)).
3. **CLI framework** — `argparse` (zero deps, fine for V1), `click` (more ergonomic), or `typer` (typed click). Propose `typer` for the cleaner per-command type signatures.
4. **Telemetry** — none in V1. (A `--share-anonymous-metrics` opt-in could come later; out of scope now.)
5. **Versioning** — semver. V1 ships as `0.1.0`; the first publicly-stable release is `1.0.0`.

---

*End of LOCAL_V1_PLAN.md v0.1.*
