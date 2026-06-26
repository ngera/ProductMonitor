# Feedback Monitor — Pluggable Platform Design

**Version:** 0.1
**Status:** Design proposal
**Supersedes scope of:** [DESIGN.md](DESIGN.md) (the personal-Windows V1 is now this platform's first reference deployment)
**Sibling docs:** [REDDIT_APPROVAL_PLAN.md](REDDIT_APPROVAL_PLAN.md)

## 1. What changed

The original [DESIGN.md](DESIGN.md) targeted a personal weekly Windows monitor. The product surface has now expanded to:

1. **Topic-agnostic.** "Windows" is one topic; "Salesforce Lightning UX", "ServiceNow Now Assist", "iPhone 17 Pro" are other topics. Per topic: own sources, own taxonomy, own sample-snippet library, own LLM routing.
2. **Pluggable sources.** Adding a new platform is writing one plugin against a stable contract, not editing the pipeline.
3. **Pluggable LLMs.** No lock-in to Phi-4-mini, Foundry Local, or any single provider. Different LLMs serve different stages of the pipeline (cheap relevance vs. expensive enrich).
4. **Two modes:** **standalone** (admin runs it themselves, browses a dashboard, configures sources, labels sample snippets) and **service** (other products — Salesforce, ServiceNow, Windows team, Facebook — call a REST API and get classified feedback for their topic).

The personal-Windows monitor in [DESIGN.md](DESIGN.md) becomes "the first reference topic configuration" running on top of this platform.

## 2. Architectural decisions and rationale

Each of these is a load-bearing call. They're called out individually so they're easy to flip later.

| # | Decision | Chosen | Alternative considered | Why |
|---|---|---|---|---|
| D1 | Plugin runtime | **In-process Python entry points** (`importlib.metadata.entry_points`) | Subprocess + JSON-RPC, gRPC sidecars, WASM | Simple, fast, debuggable. Plugins are trusted code we write. Upgrade path to subprocess exists if we ever take third-party plugins. |
| D2 | Service interface | **REST + webhooks** (FastAPI) | gRPC, GraphQL | Every named consumer (SF/ServiceNow) speaks REST natively. gRPC adds ops weight without proportional benefit at this scale. |
| D3 | Multi-tenancy | **Single-tenant deployments per consumer** | Shared multi-tenant SaaS | Cuts auth/RBAC/data-isolation scope in half. Enterprises want their own instance for compliance anyway. Multi-tenant is a later track. |
| D4 | Topic model | **Topic = first-class config unit**, includes sources, taxonomy, LLM routing, sample snippets | Hard-coded per-deployment | Required by the "any product" requirement. One process can host many topics. |
| D5 | LLM abstraction | **Capability-tagged adapters behind an `LLMRouter`** | One mega-adapter per provider | Lets each pipeline stage pick the right model independently. Adapters are small. |
| D6 | Standalone storage | **DuckDB + JSONL + SQLite** (current stack) | Postgres-only | Keeps personal-mode lightweight. Service mode uses Postgres + S3 via the same storage interface. |
| D7 | Admin UI | **FastAPI + HTMX + Jinja**, server-rendered | React SPA, Streamlit | Matches project minimalism. HTMX is enough for an admin tool. |
| D8 | Classification schema | **Generic core + per-topic `extras` schema** | One global Pydantic schema | The current `is_windows_relevant`, `windows_major` etc. become topic-specific; the core schema is product-neutral. |
| D9 | Sample-snippet feature | **Dual-purpose: few-shot examples *and* eval gold** | Separate features | Same labeled data does two jobs; doubles the value of admin labeling effort. |
| D10 | Language | **Python 3.11+** end-to-end | Polyglot (Go service, Python pipeline) | Plugin interface is Python ABC; service is FastAPI. Keep one runtime. |

## 3. High-level architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          Admin UI  (FastAPI + HTMX)                     │
│   topics · sources · taxonomies · LLM routing · snippets · reports      │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
┌─────────────────────────────────────────────────────────────────────────┐
│                      Service API  (FastAPI REST + webhooks)             │
│  POST /topics  POST /runs  GET /runs/{id}  GET /items  GET /issues …    │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
┌─────────────────────────────────────────────────────────────────────────┐
│                         Core Pipeline Orchestrator                      │
│        Fetch → Normalize → Filter → Relevance → Classify → Group        │
│                       → Score → Aggregate → Render                      │
└──────┬─────────────────────────┬─────────────────────────┬──────────────┘
       │                         │                         │
┌──────▼──────┐         ┌────────▼────────┐       ┌────────▼────────┐
│  Sources    │         │   LLM Router    │       │   Storage       │
│  Registry   │         │  (capability-   │       │   Backend       │
│             │         │   tagged)       │       │                 │
│ reddit      │         │                 │       │ duckdb / jsonl  │
│ hn          │         │ foundry_local   │       │   (standalone)  │
│ github      │         │ openai          │       │ postgres / s3   │
│ rss         │         │ anthropic       │       │   (service)     │
│ stackex     │         │ gemini          │       │                 │
│ youtube     │         │ ollama          │       └─────────────────┘
│ bluesky     │         │ perplexity      │
│ mastodon    │         │ tavily          │
│ x_paid      │         │ vllm_http       │
│ enrich_web  │         │ …               │
└─────────────┘         └─────────────────┘
```

Sources, LLMs, and storage backends are loaded via Python entry points at startup. Adding a new source means publishing a package (or dropping a folder under `plugins/sources/`) that registers an entry point — no core code changes.

## 4. Pluggable source contract

### 4.1 Lifecycle

Each source is a Python class implementing `Source` plus a `SourceManifest`. The manifest is what the admin UI reads to auto-generate configuration forms.

```python
# pipeline/contracts/source.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator, Any, Optional

@dataclass
class SourceCapabilities:
    supports_search: bool          # native keyword search
    supports_listing: bool         # firehose / new / top
    supports_comments: bool        # threaded replies
    supports_engagement: bool      # likes/upvotes/replies count
    cursor_kind: str               # "timestamp" | "opaque_token" | "page_number"
    requires_auth: bool
    requires_approval: bool        # platform-side approval (not just credentials)
    rate_limit_qpm: Optional[int]
    public_redistribution_allowed: bool  # informs report rendering

@dataclass
class SourceManifest:
    plugin_id: str                 # "reddit", "hn", "github_issues", …
    display_name: str
    version: str
    capabilities: SourceCapabilities
    config_schema: dict            # JSON Schema; admin UI renders form from this
    secrets_schema: dict           # JSON Schema for env-var-backed secrets
    default_credibility_weight: float

class Source(ABC):
    manifest: SourceManifest

    @abstractmethod
    def health_check(self) -> dict:
        """Cheap pre-run probe. Returns {'ok': bool, 'detail': str}."""

    @abstractmethod
    def fetch(
        self,
        topic_spec: "TopicSpec",
        stream_config: dict,
        cursor: "SourceCursor",
        stats: "FetchStats",
    ) -> Iterator["RawItem"]:
        """Yield items newer than cursor for ONE configured stream.
        MUST populate RawItem.url with a direct deep link.
        MAY use topic_spec.queries to drive native search."""
```

### 4.2 What plugins receive

A `TopicSpec` carries everything topic-specific: the topic display name (`"Windows 11"`, `"Salesforce Lightning"`), the search queries (`["KB5036980", "bluetooth stutter"]`), the taxonomy areas, and the LLM routing keys. Sources can use `topic_spec.queries` to drive native search APIs; sources without search ignore them and rely on the relevance gate.

### 4.3 Plugin discovery

```toml
# pyproject.toml of a source plugin package
[project.entry-points."feedback_monitor.sources"]
reddit = "feedback_monitor_reddit:RedditSource"
```

At startup the orchestrator does:
```python
from importlib.metadata import entry_points
for ep in entry_points(group="feedback_monitor.sources"):
    SourceRegistry.register(ep.load())
```

In-tree plugins live under `plugins/sources/<id>/` and are registered the same way via the package's own `pyproject.toml`. No magic discovery from filesystem.

### 4.4 Source configuration shape

Per topic, in YAML (or POST'd via the API):

```yaml
topic_id: windows-11
sources:
  - plugin_id: reddit
    instance_id: reddit-windows-subs
    enabled: true
    credibility_weight: 1.0
    streams:
      - subreddit: Windows11
        engagement_threshold: 5
        fetch_mode: triangulate+search
      - subreddit: pcmasterrace
        engagement_threshold: 20
        fetch_mode: search_only
        search_queries: ['title:windows', 'title:"windows 11"']
    secrets_ref: reddit-creds-1   # name of a secret entry, not the value

  - plugin_id: github_issues
    instance_id: github-microsoft-repos
    enabled: true
    credibility_weight: 1.2
    streams:
      - repo: microsoft/PowerToys
      - repo: microsoft/terminal
      - repo: microsoft/WSL
    secrets_ref: github-pat-1
```

## 5. Pluggable LLM contract

### 5.1 Capabilities and stages

| Capability tag | What it means | Example providers |
|---|---|---|
| `text` | Free-form completion | All |
| `json_schema` | Guided/structured output to a Pydantic schema | Foundry Local + Outlines, OpenAI strict mode, Anthropic tool_use, Gemini schema mode |
| `embed` | Returns vectors | OpenAI embeddings, BGE via local, Cohere |
| `web_search` | LLM with its own retrieval; returns answer + citations | Perplexity Sonar, Tavily, Exa, You.com, Bing Grounding |
| `vision` | Accepts images | GPT-4o, Claude, Gemini |
| `long_context` | >200k context window | Claude, Gemini |

Pipeline stages declare what capabilities they need; the router resolves each stage to an adapter that has them.

| Pipeline stage | Required capabilities | Typical model class |
|---|---|---|
| `relevance` | `text`, `json_schema` | Cheap small (Phi-4-mini, Haiku, Gemini Flash, GPT-4o-mini) |
| `classify` | `json_schema` | Mid (Phi-4 14B, Sonnet, GPT-4o-mini) |
| `enrich` | `web_search` | Perplexity Sonar / Tavily / Exa |
| `embed` | `embed` | BGE-small (V2+) |
| `summarize_issue` | `text` | Mid-tier any |
| `label_assist` | `web_search`, `text` | Perplexity / Sonar (in admin UI: "find 30 candidate posts about X") |

### 5.2 Adapter contract

```python
# pipeline/contracts/llm.py

class LLMAdapter(ABC):
    manifest: LLMAdapterManifest      # provider_id, capabilities, cost-per-token

    @abstractmethod
    def health_check(self) -> dict: ...

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Unified request:
           - prompt or messages
           - optional pydantic_schema (engages json_schema capability)
           - optional web_search=True (engages web_search capability)
           - temperature, seed, max_tokens, timeout
           Returns: text, parsed_obj (if schema), citations (if web_search),
                    usage (tokens in/out/cost), provider_request_id.
        """
```

### 5.3 Routing config

```yaml
llm_routing:
  relevance:
    adapter: foundry_local
    model: phi-4-mini
    temperature: 0
    seed: 42
  classify:
    adapter: foundry_local
    model: phi-4-mini
    fallback:
      adapter: anthropic
      model: claude-haiku-4-5-20251001   # use only when local fails health check
  enrich:
    adapter: perplexity
    model: sonar-pro
    monthly_budget_usd: 25
  embed:
    adapter: foundry_local
    model: bge-small-en
  label_assist:
    adapter: perplexity
    model: sonar
```

### 5.4 Why this matters operationally

- Swap a provider by editing routing config — no code change.
- Cost gating per stage: `monthly_budget_usd` is enforced by the router; over-budget stages downgrade or fail loud.
- Provider outages don't take the whole pipeline down — `fallback` chains absorb them.
- Every LLM call logs to a `llm_calls` table: stage, adapter, model, tokens, cost, latency, response excerpt. Becomes the basis for cost reports and prompt-drift detection.

## 6. Topic-agnostic data model

### 6.1 What stays generic

`RawItem` (in [pipeline/models.py](../pipeline/models.py)) is already source-agnostic. Keep it.

### 6.2 What needs to change

The current `Classification` in [pipeline/models.py:99-129](../pipeline/models.py#L99-L129) hardcodes Windows fields (`is_windows_relevant`, `windows_major`, `windows_build`, …). Split into:

```python
class CoreClassification(BaseModel):
    is_topic_relevant: bool                    # was: is_windows_relevant
    areas: list[str]                           # from topic.taxonomy.areas
    content_types: list[str]
    sentiment: float
    summary: str
    confidence: float
    user_context: str
    entities: list[Entity]
    extras: dict[str, Any] = Field(default_factory=dict)   # per-topic fields
```

The topic config supplies an `extras_schema` (Pydantic class loaded from the topic plugin or YAML) that defines the topic-specific fields:

```yaml
# topics/windows-11/topic.yaml
topic_id: windows-11
display: Windows 11
extras_schema_ref: feedback_monitor_topics.windows:WindowsExtras
```

`WindowsExtras` is the old `windows_*` fields. A `salesforce-lightning` topic would define `lightning_release`, `org_edition`, etc. The classifier prompt is templated from `core_fields + topic.extras_schema` so the LLM emits a single JSON object.

Entity extraction stays generic — `(type, vendor, product, version, role, verbatim)` works for any topic — but the `vendors.yaml` per-topic seed list changes.

### 6.3 Taxonomy as topic-scoped

```yaml
# topics/salesforce-lightning/taxonomy.yaml
version: "2026-06-25"
areas:
  - id: list_views
    display: List Views
    keywords: [list view, list views, view, filter]
  - id: lwc
    display: Lightning Web Components
    keywords: [lwc, lightning component]
  - id: lightning_app_builder
    display: Lightning App Builder
    keywords: [app builder, lightning page]
```

Trend continuity rules from [DESIGN.md §6.2](DESIGN.md#L549) (taxonomy versioning, discontinuity markers) carry over per-topic.

## 7. Sample snippets — the new admin feature

The user requested: *"an administrator should be able to … add snippets of sample articles, posts, etc as what good looks like."*

### 7.1 Two jobs from one labeling effort

A snippet labeled in the admin UI is used for:

1. **Few-shot examples in LLM prompts** — relevance and classify prompts include 2–4 in-topic snippets with their labels. Improves accuracy without fine-tuning, and demonstrably reduces drift when the model changes.
2. **Eval gold for the eval harness** ([DESIGN.md §7](DESIGN.md#L619)). Same snippet, same label, but used to grade outputs rather than seed them. The harness reserves a held-out slice (default 30%) so few-shot use doesn't contaminate eval.

### 7.2 Schema

```sql
CREATE TABLE topic_examples (
    example_id        VARCHAR PRIMARY KEY,
    topic_id          VARCHAR NOT NULL,
    source_url        VARCHAR,             -- nullable; pasted text is allowed
    title             VARCHAR,
    body              TEXT NOT NULL,
    labels_json       VARCHAR NOT NULL,    -- the full Classification as authored
    polarity          VARCHAR NOT NULL,    -- 'positive_example' | 'negative_example'
    holdout_eval      BOOLEAN NOT NULL,    -- reserved from few-shot use
    created_by        VARCHAR,
    created_at        TIMESTAMP,
    notes             TEXT
);
```

`positive_example` = "this IS relevant and should be classified this way."
`negative_example` = "this looks topical but is NOT in scope" (used to harden the relevance gate against false positives).

### 7.3 Admin labeling UX (standalone mode)

Three entry points in the admin UI:

- **Paste text** — admin pastes a snippet directly. Form auto-fills the label form with the LLM's current best guess; admin corrects it.
- **Paste URL** — admin pastes a real source URL (e.g. a Reddit thread). The pipeline fetches the raw item, the LLM produces a draft label, admin corrects.
- **From the dashboard** — every item shown on a report has a "Label this as a good example" button that opens the same labeler with the item pre-filled.

This solves the cold-start problem: an admin can stand up a new topic and seed 50 examples in an evening without any code.

### 7.4 Suggested-example mining (label-assist via web-search LLM)

The admin clicks "find more examples for `area: bluetooth_audio`" → the label-assist LLM runs a web-search query and returns 10 candidate URLs. The admin reviews and accepts. This is the [Reddit approval plan's](REDDIT_APPROVAL_PLAN.md) "Step 5 — what to do while you wait" path, productized.

## 8. Service mode

### 8.1 REST surface (V1)

```
# Topics
POST   /v1/topics                   create
GET    /v1/topics/{id}              read
PATCH  /v1/topics/{id}              update
POST   /v1/topics/{id}/runs         trigger a run (idempotent via Idempotency-Key)
GET    /v1/topics/{id}/runs         list
GET    /v1/runs/{run_id}            run status, counters, completeness

# Data
GET    /v1/topics/{id}/items        filtered items (paginated)
GET    /v1/topics/{id}/issues       grouped issues
GET    /v1/topics/{id}/reports/{week_id}   rendered report JSON

# Snippets (label store)
POST   /v1/topics/{id}/examples     add a sample snippet
GET    /v1/topics/{id}/examples     list

# Webhooks
POST   /v1/topics/{id}/webhooks     register
# Events: run.completed, issue.new, issue.resurfaced, run.failed
```

### 8.2 Auth

API key per consumer, passed as `Authorization: Bearer <key>`. Keys are scoped to one or more topics so a tenant can't read another's data.

### 8.3 Idempotency

`POST /runs` accepts `Idempotency-Key: <uuid>` — duplicate requests in a 24h window return the same run record. Critical for callers that retry on flaky networks (SF/SN both do this).

### 8.4 Sample consumer flow (Salesforce)

```
1. Salesforce admin creates an API key in the feedback-monitor admin UI
2. SF posts: POST /v1/topics  body: {id: "sf-lightning", taxonomy: {…}, sources: […]}
3. SF posts sample snippets:  POST /v1/topics/sf-lightning/examples
4. SF triggers weekly run:    POST /v1/topics/sf-lightning/runs
5. SF subscribes webhook:     POST /v1/topics/sf-lightning/webhooks  url=<SF callback>
6. Run completes → webhook fires → SF pulls /reports/<week_id> → renders inside SF
```

### 8.5 Multi-tenancy stance (recap of D3)

Each consumer runs their own deployment. The service runs N topics within that deployment but does NOT cross customers. A "true SaaS multi-tenant" version is a later track that needs tenant-isolated storage, RBAC, audit logs — all of [DESIGN.md Appendix A](DESIGN.md#L928).

## 9. Standalone mode

For users who run the service themselves:

- Same FastAPI app as service mode, but with the admin UI mounted at `/`.
- Single sign-on optional; default is local password file.
- The pipeline can also be invoked from CLI: `feedback-monitor run --topic windows-11 --week 2026-W26`.
- Reports are written to disk AND served via the admin UI's report viewer.

## 10. Per-source access & implementation plan

Each section below: **what it takes to get access**, **what the plugin does**, **default config shape**, **realistic time-to-first-data**.

### 10.1 Reddit

- **Access**: see [REDDIT_APPROVAL_PLAN.md](REDDIT_APPROVAL_PLAN.md). Personal Script app + non-commercial approval. 2–4 weeks.
- **Plugin**: wraps `praw`. Existing [sources/reddit.py](../sources/reddit.py) is ~80% of the plugin. Refactor into the new contract.
- **Capabilities**: search ✓, listing ✓, comments ✓, engagement ✓.
- **Time to first data**: 1 day of refactor + 2–4 weeks wait.

### 10.2 Hacker News (Algolia API)

- **Access**: none required. Public HTTP API.
- **Endpoint**: `https://hn.algolia.com/api/v1/search_by_date?query={q}&tags=story,comment&numericFilters=created_at_i>{cursor}`
- **Plugin**: pure `httpx`. ~150 LOC.
- **Capabilities**: search ✓, listing ✓ (via empty query), comments ✓, engagement ✓ (points).
- **Config**: list of search queries from `topic_spec.queries`.
- **Time to first data**: 1 day.

### 10.3 GitHub Issues

- **Access**: personal access token (PAT), free, self-issued. No approval.
  - Create at `github.com/settings/tokens` → fine-grained PAT → read access on public repos.
  - Rate limit with PAT: 5,000 req/hr.
- **Plugin**: REST via `httpx` or `PyGithub`. Per repo: `GET /repos/{owner}/{repo}/issues?since=…&state=all` then `GET /issues/{n}/comments` for thread.
- **Capabilities**: search ✓ (via `/search/issues`), listing ✓, comments ✓, engagement ✓ (reactions).
- **Config**:
  ```yaml
  streams:
    - repo: microsoft/PowerToys
    - repo: microsoft/terminal
    - repo: microsoft/WSL
  include_labels: []   # empty = all
  exclude_labels: [duplicate, wontfix]
  ```
- **Time to first data**: 1–2 days.

### 10.4 Microsoft Tech Community + Microsoft Q&A (RSS)

- **Access**: none required.
- **Endpoint**: per-board RSS, e.g. `https://techcommunity.microsoft.com/category/windows/rss`. Q&A: `https://learn.microsoft.com/answers/tags/{tag}/feed`.
- **Plugin**: `feedparser` for RSS parse; lightweight HTML scrape for comments if needed.
- **Capabilities**: listing ✓, comments ✗ (RSS gives top-level only), engagement ~ (Likes count not in RSS; scrape if needed).
- **Time to first data**: 1 day.

### 10.5 Stack Exchange (Stack Overflow, Super User, Server Fault)

- **Access**: anonymous works at 300 req/day. Free app key at `stackapps.net/apps/oauth/register` raises to 10,000 req/day. No approval.
- **Endpoint**: `https://api.stackexchange.com/2.3/search/advanced?site=superuser&tagged=windows-11&fromdate={cursor}`
- **Plugin**: pure `httpx`. Single endpoint covers search + tag listing.
- **Capabilities**: search ✓, listing ✓ (by tag), comments ~ (answer threading, no nested comments without extra calls), engagement ✓ (score, accepted_answer).
- **Config**:
  ```yaml
  streams:
    - site: superuser
      tags: [windows-11, windows-10]
    - site: stackoverflow
      tags: [winapi]
  ```
- **Time to first data**: 1 day.

### 10.6 RSS news / blogs (generic)

- **Access**: none required.
- **Plugin**: one plugin with N feed URLs in config. `feedparser` + `httpx`.
- **Capabilities**: listing ✓ (chronological), search ✗ (you don't filter by query, you classify after).
- **Config**:
  ```yaml
  streams:
    - feed_url: https://www.windowscentral.com/rss.xml
    - feed_url: https://www.bleepingcomputer.com/feed/category/microsoft/
    - feed_url: https://www.neowin.net/news/rss
  default_credibility_weight: 0.7   # context, not primary signal
  ```
- **Time to first data**: half a day.

### 10.7 YouTube comments (Data API v3)

- **Access**: Google Cloud project + API key. Free. No approval but quota: 10,000 units/day. Comments list = 1 unit, search = 100.
- **Strategy**: hard-code a channel allow-list per topic. Poll `playlistItems` for uploads. Fetch `commentThreads` only on videos matching keyword in title. Never use `search`.
- **Plugin**: `google-api-python-client` or raw `httpx`. ~250 LOC.
- **Capabilities**: comments ✓, engagement ✓ (likes), listing ~ (by channel).
- **Config**:
  ```yaml
  streams:
    - channel_id: UCBJycsmduvYEL83R_U4JriQ   # MKBHD
      title_filter: ["windows", "surface", "copilot+ pc"]
    - channel_id: UCXuqSBlHAE6Xw-yeJA0Tunw   # Linus Tech Tips
  ```
- **Time to first data**: 2 days.

### 10.8 Bluesky (AT Protocol)

- **Access**: app password (free, no review). OAuth is the recommended new path but app passwords still work.
- **Endpoint**: `app.bsky.feed.searchPosts` (requires auth).
- **Plugin**: `atproto` Python client.
- **Capabilities**: search ✓ (auth required), listing ✓ (firehose), engagement ✓.
- **Time to first data**: 1 day. **Volume on most topics is currently low** — add but don't expect signal yet.

### 10.9 Mastodon (fediverse)

- **Access**: register an app on one or more instances. Free. No central approval.
- **Endpoint**: `/api/v2/search?q=…&type=statuses` per instance.
- **Plugin**: `Mastodon.py`. Iterates over a configured instance list.
- **Capabilities**: search ✓ (per-instance), listing ✓ (timelines), engagement ✓.
- **Time to first data**: 1–2 days.

### 10.10 X / Twitter

- **Access**: pay-per-use, no free read tier for new developers as of Feb 2026. ~$0.005 per post read; ~$30–100/month for modest weekly volume.
- **Plugin**: `tweepy` or raw HTTP.
- **Capabilities**: search ✓, listing ✓, engagement ✓.
- **Default**: disabled in topic config. Admin opts in per topic with a budget cap.
- **Time to first data**: 1 day after billing setup.

### 10.11 TikTok

- **Access**: Research API — academic/EU-nonprofit only. Personal use ineligible.
- **Plugin**: stub manifest declares `requires_approval=true`, `requires_affiliation=true`. Admin UI shows: "Eligibility: academic / EU non-profit only. Cannot enable from this UI."
- **Implementation**: deferred until a deployment has eligibility. The plugin shell + manifest is worth shipping as documentation.

### 10.12 Facebook / Meta

- **Access**: two paths.
  - **Meta Content Library** — academic/nonprofit only, same shape as TikTok. Defer.
  - **Graph API Page Public Content Access** — App Review required. Lets you read public posts from declared Pages (e.g. Microsoft's official Page).
- **Plugin**: `facebook-sdk` or raw Graph HTTP.
- **Capabilities**: listing ✓ (per Page only), comments ✓, search ✗.
- **Time to first data**: 2–4 weeks of App Review + 1–2 days of code. Worth it only if a topic has identifiable official Pages.

### 10.13 Web-search LLM enrichment (Perplexity / Tavily / Exa / Sonar)

This is a **virtual source**, not a primary one. Used in three roles per [previous discussion]:

1. As a `label_assist` LLM in the admin UI (find candidate URLs to label).
2. As an `enrich` LLM on each issue (one query per issue to fetch official response context).
3. As a **fallback source** for closed platforms (e.g. "is anyone on TikTok talking about KB5036980 this week?") — wrapped in a `WebSearchSource` plugin with a low credibility weight (0.3) and a UI tag that marks the items as indirect signal.

The `enrich` and `label_assist` use cases go through the LLM router, not the source contract. The `WebSearchSource` does go through the source contract — same plugin shape as everything else.

## 11. Implementation phases

The full platform is a 3–6 month build for one engineer. Phase it so each phase is independently usable.

### Phase 0 — Refactor current code to plugin shape (~2 weeks)

- Define the four core contracts: `Source`, `LLMAdapter`, `StorageBackend`, `TopicSpec`.
- Wrap existing [sources/reddit.py](../sources/reddit.py) and [pipeline/llm.py](../pipeline/llm.py) as plugins. Same behavior; new shape.
- Extract Windows-specific bits of `Classification` into a `WindowsExtras` Pydantic class loaded via topic config.
- Result: existing personal-Windows monitor still works; everything else is now possible.

### Phase 1 — Service-mode REST API + standalone admin UI shell (~3 weeks)

- FastAPI app with the `/v1/topics` and `/v1/runs` endpoints from §8.1.
- Admin UI (HTMX) with: topic CRUD, source instance CRUD, sample-snippet labeling, run trigger, run status.
- Auth: API keys + a single bootstrap admin password.
- Webhooks: outbound POST on run.completed.

### Phase 2 — Source plugins, batch 1 (~2 weeks)

- HN, GitHub Issues, Microsoft Tech Community RSS, generic RSS, Stack Exchange.
- All five share ~80% of plugin code; the per-source delta is the API/endpoint shape.

### Phase 3 — LLM router + cost telemetry (~2 weeks)

- Capability tags, adapter registry, routing config.
- Adapters for: Foundry Local OAI-HTTP, OpenAI, Anthropic, Gemini, Ollama, Perplexity Sonar.
- `llm_calls` audit table; cost dashboard in admin UI.
- Enrich stage wired in.

### Phase 4 — Source plugins, batch 2 (~2 weeks)

- YouTube comments, Bluesky, Mastodon. X behind a paid-tier toggle.
- Optional: WebSearchSource for closed-platform thin coverage.

### Phase 5 — Service-mode hardening (~3 weeks)

- Idempotency-Key support, request signing for webhooks.
- Postgres + S3 storage backend.
- Per-topic budgets (LLM cost, fetch volume) and circuit breakers.
- OpenAPI spec auto-generated from FastAPI.
- Integration test suite using a fake source plugin.

### Phase 6 — Sample-snippet feature deepening (~2 weeks)

- Held-out eval split, per-topic eval gates.
- Few-shot injection into relevance and classify prompts.
- Label-assist mining via web-search LLM in the UI.

### Phase 7 (optional, later) — Multi-tenant SaaS variant

- Tenant isolation in storage and auth.
- RBAC, audit log.
- Per-tenant cost attribution.
- Roughly the [DESIGN.md Appendix A](DESIGN.md#L928) enterprise scope, but only what's needed to share one deployment across customers.

## 12. Directory layout (target)

```
feedback_monitor/
├── pyproject.toml                # registers core entry points
├── docs/
├── core/
│   ├── contracts/                # Source, LLMAdapter, StorageBackend, TopicSpec
│   ├── orchestrator/             # pipeline stages, run loop
│   ├── registry/                 # plugin discovery
│   ├── router/                   # LLM router
│   ├── storage/
│   │   ├── duckdb_backend.py
│   │   └── postgres_backend.py
│   ├── models/                   # core Pydantic schemas
│   ├── render/                   # Jinja templates
│   └── eval/                     # eval harness
├── service/
│   ├── api/                      # FastAPI routes
│   ├── admin_ui/                 # HTMX templates + static
│   ├── webhooks/
│   └── auth/
├── plugins/
│   ├── sources/
│   │   ├── reddit/
│   │   ├── hn/
│   │   ├── github_issues/
│   │   ├── microsoft_tech_community/
│   │   ├── rss/
│   │   ├── stackex/
│   │   ├── youtube/
│   │   ├── bluesky/
│   │   ├── mastodon/
│   │   ├── x_paid/
│   │   └── web_search_source/
│   ├── llms/
│   │   ├── foundry_local/
│   │   ├── openai/
│   │   ├── anthropic/
│   │   ├── gemini/
│   │   ├── ollama/
│   │   └── perplexity/
│   └── topics/                   # per-topic extras schemas
│       ├── windows/
│       └── salesforce_lightning/
├── deployments/
│   ├── standalone/               # docker-compose for self-host
│   └── service/                  # docker-compose + helm
└── tests/
```

## 13. Migration path from current code

The current code is not wasted. Map:

| Current | Target |
|---|---|
| [sources/reddit.py](../sources/reddit.py) | `plugins/sources/reddit/` (lift, add `manifest`) |
| [sources/base.py](../sources/base.py) | `core/contracts/source.py` (extend with `manifest` + capabilities) |
| [pipeline/run.py](../pipeline/run.py) | `core/orchestrator/run.py` |
| [pipeline/llm.py](../pipeline/llm.py) | `plugins/llms/foundry_local/` + `core/router/` |
| [pipeline/models.py](../pipeline/models.py) `Classification.windows_*` | `plugins/topics/windows/extras.py` |
| [config/sources.yaml](../config/sources.yaml) | `topics/windows-11/topic.yaml` (renamed, restructured) |
| [eval/](../eval/) | `core/eval/` + `topics/windows-11/golden_set.jsonl` |

The personal-Windows monitor keeps working at every phase boundary. Phase 0's exit criterion is: existing weekly run produces an identical report after the refactor.

## 14. What this design defers

- True multi-tenant SaaS (Phase 7).
- Embedding-based clustering (V2 of original design — folds into Phase 6 or later).
- Active-learning loop for snippet curation.
- A11y review of admin UI.
- SSO for admin UI (local password first).
- Compliance certifications (SOC 2 etc.) — only meaningful at Phase 7.
- A reasoning-model fallback for the classify stage when local LLM is uncertain.

## 15. Open questions to resolve before Phase 0

1. **Service hosting model**: do consumers self-host, or do we operate it? Affects packaging.
2. **Plugin security model**: do we accept third-party plugins, or only first-party? Affects whether D1 (in-process) needs to flip to subprocess.
3. **Admin auth**: local password sufficient for V1, or SSO required for the first enterprise customer?
4. **Snippet license**: when admins paste a snippet, who owns it? Need a TOS for that.
5. **PII handling**: even non-commercial Reddit terms require respecting deletions. Service mode needs a deletion API surface and a worker that propagates deletions across raw, warehouse, and report layers.

---

*Save point: this doc is the contract for what we build. Future plans (`SOURCE_<name>_PLAN.md`, `LLM_<provider>_PLAN.md`) hang off it.*
