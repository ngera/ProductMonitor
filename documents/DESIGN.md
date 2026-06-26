# Customer Feedback Monitor — Design & Implementation Plan

**Version:** 0.3
**Owner:** Personal project
**Target platform:** Windows 11, manual weekly execution
**Status:** Design

## Changes from v0.2

This revision tightens scope dramatically and incorporates technical feedback. Key changes:

1. **V1 cut to minimum viable loop:** fetch → classify → group-by-area-and-KB → static report. No embeddings, no Flask, no admin UI.
2. **Eval-first:** the very first thing built is a hand-labeled golden set and an eval harness. No pipeline code is written before there is a measurable target.
3. **Two-call classifier with guided decoding:** replaces the single mega-prompt. Cheap relevance call gates expensive extraction.
4. **Deterministic grouping replaces clustering in V1:** issues are keyed on `(area, KB_number)` and `(area, primary_entity)`. Embedding-based clustering is V2.
5. **Honest idempotency story:** only Fetch and Normalize are reproducible. Classification is non-deterministic; clustering will be path-dependent when it lands in V2. Raw JSONL is the only true rebuild guarantee.
6. **Append-only issue identity:** once an item is attached to an issue, that assignment is permanent. `issue_id` is never reassigned. Past weeks are read-only.
7. **Schema fixes:** `classifications` split into per-item and per-area tables; `entity_mentions` PK includes `type` and handles null products.
8. **Reddit completeness fixes:** triangulate via `.new()` + `.top()` + `.controversial()`; fetch all comments for relevant posts (drop engagement threshold for comment fetch); explicit logging of pagination ceiling hits.
9. **Taxonomy versioning:** every weekly rollup records the taxonomy version it was computed under; trend charts show a visible discontinuity marker across version changes.
10. **Versioned roadmap (V1 → V4):** each version unlocks one coherent capability. You can stop at any version and have a working system.

---

## 1. Executive Summary

A locally-run system that gathers Windows-related user feedback from public sources (Reddit in V1), classifies each item along functional area, content type, severity, and Windows version, extracts third-party entities (hardware, drivers, KB/CVE numbers, repro steps), and groups items into issues using deterministic keys. Outputs a static weekly HTML report with full source attribution and primary/duplicate linkage.

Built around a small local LLM (Phi-4-mini via Foundry Local) with constrained JSON decoding and a hand-labeled eval set as a gate on quality. Designed to grow from a one-person Sunday-morning tool into a richer system through five well-defined versions, but to be fully usable at V1.

---

## 2. Goals & Non-Goals

### Goals (V1)

- Pull Windows-relevant items from Reddit weekly with no silent data loss.
- Classify each item along four dimensions (area, content type, severity, context) with measured accuracy.
- Extract repro steps, third-party entities, and KB numbers; preserve verbatim source phrasing.
- Group items into issues using deterministic keys; primary item identified per issue.
- Detect recurring issues across weeks via KB-number and primary-entity matching.
- Generate static HTML weekly reports with every item linked back to its original URL.
- Hand-labeled golden set + eval harness measuring per-dimension precision/recall.

### Non-Goals (V1)

- Web UI / dashboard / admin pages — config is hand-edited YAML, reports are static files.
- Embedding-based similarity / clustering — deterministic grouping only.
- Multiple sources — Reddit only.
- Cross-issue relationship inference, vendor analytics page, novel-keyword detection.
- Multi-user, networked, or cloud deployment.

### Success Criteria (V1)

- A weekly run completes without manual intervention.
- Eval harness reports ≥0.75 F1 on each classification dimension against the golden set.
- Reports surface known real issues correctly tagged with the right area, entities, and KB numbers.
- No silent data loss: every dropped item is logged with reason.
- A second source could be added in V4 without changes to the pipeline core.

---

## 3. Versioned Roadmap

Each version unlocks one coherent capability and is independently usable.

| Version | Scope | Effort | Unlocks |
|---|---|---|---|
| **V1** | Fetch → classify → KB/entity grouping → static HTML report. Hand-edited YAML. Eval harness. | ~2 weeks | A working weekly tool. |
| **V1.5** | Cross-week issue continuity using deterministic keys. Resurface tracking. Per-issue HTML pages. | ~1 week | "This is the same issue we saw 3 weeks ago." |
| **V2** | Embedding-based clustering for issues with no clean deterministic key. Greedy nearest-pair matching. | ~1.5 weeks | Catch the long-tail of duplicates the deterministic key misses. |
| **V2.5** | Flask web UI: dashboard, report browser, filters. YAML still hand-edited. | ~1.5 weeks | Interactive browsing, filtered slices. |
| **V3** | Admin UI for sources/taxonomy/vendors. Taxonomy versioning for trend continuity. Vendor analytics page. | ~2 weeks | Self-service config without touching files. |
| **V4+** | Additional sources (RSS, HN, Bluesky, GitHub Issues). Source abstraction validated. | ~1 week per source | Wider signal. |

V1 is the only commitment. Each later version is built only when V_n is in regular use and the added value is concrete.

---

## 4. V1 Architecture

### 4.1 Pipeline Stages

```
                      ┌────────────────────────────┐
                      │   config/*.yaml            │
                      │   (hand-edited)            │
                      └─────────────┬──────────────┘
                                    │
   ┌────────┐   ┌──────────┐   ┌────▼─────┐   ┌──────────┐   ┌──────────┐
   │ Fetch  │──▶│Normalize │──▶│ Filter   │──▶│ Relevance│──▶│ Classify │
   │(Reddit)│   │+JSONL out│   │ (Stage A)│   │  (LLM)   │   │+Extract  │
   └────────┘   └──────────┘   └──────────┘   └──────────┘   └────┬─────┘
                                                                  │
                          ┌────────┐   ┌────────────┐   ┌─────────▼──┐
                          │ Render │◀──│ Aggregate  │◀──│ Group      │
                          │ (HTML) │   │            │   │(determinist│
                          └────────┘   └────────────┘   │ic)         │
                                                        └────────────┘
```

Two LLM stages, not one. The full extraction call only runs on items the cheap relevance call accepts.

### 4.2 Source Abstraction

`sources/base.py`:

```python
class Source(ABC):
    name: str

    @abstractmethod
    def fetch_since(self, cursor: SourceCursor, config: dict) -> Iterator[RawItem]:
        """Yield items newer than cursor. Update cursor as you go.
        Must populate RawItem.url with a direct deep link to the original."""
```

Trivial for V1 (only Reddit), but the contract is in place for V4.

### 4.3 Fetch — Reddit Connector

Per subreddit in `sources.yaml`:

1. **Triangulate to mitigate the 1000-item ceiling:**
   - `subreddit.new(limit=1000)` — newest items.
   - `subreddit.top(time_filter="week", limit=100)` — top of week.
   - `subreddit.controversial(time_filter="week", limit=50)` — controversial of week.
   - Union by `external_id`; dedup by `seen_ids` table.

2. **Surface ceiling hits:** if `.new(limit=1000)` returned 1000 items AND the oldest was still newer than the cursor, log a `ceiling_hit` warning in the run record with the gap delta.

3. **Comments:** for every relevant post (no engagement threshold), fetch the full comment tree via `submission.comments.replace_more(limit=None)` then `submission.comments.list()`. Drop the engagement threshold for comment fetch — at the volumes we'll see, completeness > cost. The threshold remains only for what counts as "relevant enough to filter into Classify."

4. **URLs:** `https://reddit.com{permalink}` for both posts and comments. Both are deep-linkable.

5. **Rate limits:** praw handles by default. Add a manual sleep between subreddits to be polite.

### 4.4 Normalize & Filter (Stage A — heuristic only)

- Raw JSONL → `items` table.
- Drop: body < 50 chars without informative title; deleted/removed; canonical-URL duplicates; title simhash duplicates.
- Engagement threshold: keep items where `upvotes ≥ T` OR `comment_count ≥ T` OR matches a watchlist regex (KB number, CVE, named feature). Default T = 5 for V1; we'd rather over-include and let the LLM gate.

No LLM here. Filter is cheap and conservative.

### 4.5 Relevance Gate (cheap LLM call)

The single biggest cost lever in the pipeline. Items surviving Stage A go through a relevance-only call before expensive extraction.

**Prompt (small):**

```
Is this post about Microsoft Windows (the operating system) — including its
features, apps, drivers, updates, or user experience?

Reply with a single JSON object: {"relevant": true|false, "confidence": 0.0-1.0}

Title: {title}
Body: {body[:1000]}
```

- Guided/JSON-schema-constrained decoding if Foundry Local supports it.
- Items with `relevant=false AND confidence ≥ 0.7` are dropped (logged with reason).
- Borderline items (`confidence < 0.7`) pass through to Classify and are re-evaluated there.

Why this matters: on Tier-3 subs (r/pcmasterrace, r/mildlyinfuriating) maybe 5–15% of items survive Stage A but are off-topic. Paying full extraction cost on those wastes most of the budget.

### 4.6 Classify + Extract (full LLM call, guided decoding)

One call per relevant item. Output schema is large but **flat where possible** — no deeply nested conditionals — and emitted under **guided/constrained decoding** so the model can only produce schema-valid output. If Foundry Local doesn't expose constrained decoding, we use Outlines or llguidance externally.

**Output schema (Pydantic):**

```python
class Classification(BaseModel):
    # Core
    is_windows_relevant: bool
    areas: list[str]                          # multi-label, from taxonomy.yaml
    content_types: list[str]                  # multi-label, controlled vocab
    sentiment: float                          # -1.0 to 1.0
    summary: str                              # ≤ 200 chars
    confidence: float                         # 0.0 to 1.0

    # Always-attempted context
    user_context: str                         # consumer | enterprise | developer | power_user | unknown
    windows_major: str                        # win10 | win11 | win_server | unknown
    windows_feature_update: Optional[str]     # 22H2 | 23H2 | 24H2 | 25H2 | null
    windows_build: Optional[str]              # "26100.4061" or null
    windows_channel: Optional[str]            # stable | release_preview | beta | dev | canary | null
    windows_version_confidence: str           # explicit | inferred | unknown

    # Bug-specific (filled only if "bug_report" in content_types)
    bug_severity: Optional[str]               # critical | high | medium | low
    bug_is_regression: Optional[bool]
    bug_reproducibility: Optional[str]
    bug_repro_steps_quality: Optional[str]    # detailed | partial | none
    bug_repro_steps: Optional[list[str]]
    bug_preconditions: Optional[list[str]]

    # Request-specific (filled only if "feature_request" in content_types)
    request_specificity: Optional[str]
    request_existing_workaround: Optional[bool]

    # Entities
    entities: list[Entity]
```

**Two-call structure justification:** an alternative was three calls (relevance, classify, entities). I argued for two because (a) once you've decided to extract, you may as well extract entities in the same pass — the model has the text in context, additional tokens are cheap; (b) three calls triples coordination overhead for marginal accuracy gain. Revisit at eval time.

**Guided decoding is non-negotiable.** If Foundry Local doesn't support it, run the model via `llama-cpp-python` or `Outlines` directly. The risk of free-form JSON from a 3.8B model is too high.

**Validate-and-repair fallback:** if guided decoding isn't available and a response fails Pydantic validation, run a single repair call passing the malformed output and the validation error back to the model. If the repair still fails, mark the item `classification_failed` and continue.

### 4.7 Entity Extraction (hybrid regex + LLM)

1. **Regex pre-pass against `vendors.yaml`:** vendor names, product names, KB numbers (`KB\d{7}`), CVEs (`CVE-\d{4}-\d+`), Windows build numbers (`\d{5}\.\d+`).

2. **LLM pass (in same call as classification):** hints from regex are passed in the prompt; the model produces the final `entities` list with `(type, vendor, product, version, role, verbatim)`. The model can discard false positives and add what regex missed.

3. **Regex extractions stored separately** in `regex_extractions` for traceability — independently queryable.

```python
class Entity(BaseModel):
    type: str          # gpu | cpu | laptop | driver | audio_technology | ... (controlled vocab)
    vendor: str
    product: Optional[str]
    version: Optional[str]
    role: str          # feature_implicated | hardware_in_use | software_in_use
    verbatim: str      # exact source phrasing
```

### 4.8 Deterministic Grouping (V1's "issue" model)

No embeddings. Items are grouped into issues by deterministic keys.

**Grouping logic per area, per week:**

1. For each item, compute its `group_keys`:
   - `kb:{area}:{KB_number}` for every KB number in `regex_extractions.kb_numbers`
   - `entity:{area}:{vendor}:{product}` for the primary entity (highest-confidence entity with `role=feature_implicated`), if one exists
   - `title:{area}:{title_simhash}` as a fallback (last resort, only if no KB and no primary entity)

2. Items sharing any group_key in the same week form a candidate issue group.

3. The **canonical (primary)** item per group is selected by:
   ```
   canonical_score = item_score * 0.4
                   + repro_quality_score * 0.3
                   + body_length_score * 0.1
                   + engagement_score * 0.2
   ```

4. Singletons (items with no peer for any key) are themselves single-member issues.

**Why this works for V1:** KB numbers and named entities are precisely the high-signal cases users want grouped. Two reports of "Bluetooth stutter after KB5036980" are now correctly one issue. Three reports about "Intel AX211 driver problems" are correctly one issue. The fallback for items with neither is a singleton, which is honest — we don't pretend to know they're related when we don't have evidence.

**What this misses:** semantically-similar issues with no shared KB and no shared entity (e.g., "audio cuts out randomly" reported five different ways with no specific hardware mentioned). These will appear as five separate issues in V1. That's a known limitation; V2's embedding-based clustering catches them.

### 4.9 Cross-Week Issue Identity — V1.5

In V1, each weekly run is independent — an "issue" is a per-week grouping only. There is no cross-week issue table yet. Reports compare WoW by counting items, not issues.

V1.5 adds:
- A persistent `issues` table keyed by `(area, group_key)` — a stable hash of the deterministic key.
- Issue identity is **append-only**: once an item is attached to an issue, the assignment is permanent. Past weeks are read-only.
- `resurface_count`, `total_dormant_weeks`, `longest_dormant_streak` derived from an `issue_state_transitions` audit log.

This makes the issue lifecycle (active / dormant / resurfaced) work with a deterministic key — no clustering needed.

### 4.10 Scoring

Per-item:

```
score = log(1 + upvotes + 2*comments)
      * source_credibility_weight       # from sources.yaml
      * exp(-age_days / 7)               # recency decay
      * confidence                       # from LLM
```

Per-issue (V1.5+):

```
issue_score = sum(member_item_scores)
            * log(1 + distinct_authors)
            * log(1 + weeks_active)
```

`distinct_authors` factor prevents a single chatty user from inflating an issue.

### 4.11 Reporting (Static HTML)

V1 outputs static HTML to `reports/<week_id>/`:

```
reports/2026-W21/
  index.html               # main weekly report
  area_audio.html          # per-area detail
  area_camera.html
  ...
  comments_audio.html      # every item in area with source links
  ...
  data/                    # JSON for any charts
```

No Flask. Files are openable directly in a browser. V2.5 adds the Flask layer for navigation and filtering.

**Source attribution requirement** is unchanged from v0.2: every item-displaying template includes a shared `_item_attribution.html.j2` partial with `<source_display_name> · @<author> · <relative_time> · [direct link]`. Build-time validator fails if any template skips it.

**XSS safety:** Jinja autoescape is enabled globally for `.html` templates. Reddit-authored content (title, body, snippets) is rendered as text, never as `|safe`. The "expand to show full body" detail row uses `body|e` explicitly.

---

## 5. Data Model

### 5.1 RawItem (in-memory + JSONL line)

```python
@dataclass
class RawItem:
    source: str                   # "reddit"
    source_display_name: str      # "r/Windows11"
    external_id: str
    url: str                      # REQUIRED — direct deep link
    parent_external_id: Optional[str]
    author: Optional[str]
    created_at: datetime
    title: Optional[str]
    body: str
    engagement: dict
    raw: dict
```

### 5.2 DuckDB Tables (V1)

Schema corrections from v0.2 feedback:

```sql
-- Normalized items
CREATE TABLE items (
    id                   VARCHAR PRIMARY KEY,    -- "{source}:{external_id}"
    source               VARCHAR NOT NULL,
    source_display_name  VARCHAR NOT NULL,
    external_id          VARCHAR NOT NULL,
    url                  VARCHAR NOT NULL,
    parent_id            VARCHAR,
    author               VARCHAR,
    created_at           TIMESTAMP NOT NULL,
    fetched_at           TIMESTAMP NOT NULL,
    week_id              VARCHAR NOT NULL,
    title                VARCHAR,
    body                 TEXT NOT NULL,
    engagement_json      VARCHAR,
    raw_ref              VARCHAR,
    filter_status        VARCHAR,
    relevance_score      DOUBLE,
    is_relevant          BOOLEAN
);

-- Per-item classification (the v0.2 normalization bug fix)
-- One row per item, regardless of how many areas
CREATE TABLE item_classifications (
    item_id              VARCHAR PRIMARY KEY,
    content_types_json   VARCHAR,         -- JSON array of content type strings
    sentiment            DOUBLE,
    summary              VARCHAR,
    confidence           DOUBLE,
    is_relevant          BOOLEAN,
    model                VARCHAR,
    classified_at        TIMESTAMP
);

-- Multi-label area assignment (just the link)
CREATE TABLE item_areas (
    item_id              VARCHAR NOT NULL,
    area                 VARCHAR NOT NULL,
    PRIMARY KEY (item_id, area)
);

-- Bug attributes (one per item, if applicable)
CREATE TABLE bug_attributes (
    item_id              VARCHAR PRIMARY KEY,
    severity             VARCHAR,
    is_regression        BOOLEAN,
    reproducibility      VARCHAR,
    repro_steps_quality  VARCHAR,
    repro_steps_json     VARCHAR,
    preconditions_json   VARCHAR
);

-- Request attributes (one per item, if applicable)
CREATE TABLE request_attributes (
    item_id                       VARCHAR PRIMARY KEY,
    specificity                   VARCHAR,
    existing_workaround_mentioned BOOLEAN
);

-- Context (one per item)
CREATE TABLE item_context (
    item_id                        VARCHAR PRIMARY KEY,
    user_context                   VARCHAR,
    windows_version_major          VARCHAR,
    windows_version_feature_update VARCHAR,
    windows_version_build          VARCHAR,
    windows_version_channel        VARCHAR,
    windows_version_confidence     VARCHAR
);

-- Entity mentions (PK fix: includes type, NULL product handled)
CREATE TABLE entity_mentions (
    item_id       VARCHAR NOT NULL,
    type          VARCHAR NOT NULL,
    vendor        VARCHAR NOT NULL,
    product_key   VARCHAR NOT NULL,   -- COALESCE(product, '__unknown__')
    role          VARCHAR NOT NULL,
    product       VARCHAR,            -- nullable display value
    version       VARCHAR,
    verbatim      VARCHAR,
    PRIMARY KEY (item_id, type, vendor, product_key, role)
);
CREATE INDEX idx_entity_vendor ON entity_mentions(vendor);
CREATE INDEX idx_entity_type_vendor ON entity_mentions(type, vendor);

-- Regex extractions (one per item)
CREATE TABLE regex_extractions (
    item_id         VARCHAR PRIMARY KEY,
    kb_numbers      VARCHAR,        -- JSON array
    cve_ids         VARCHAR,
    build_numbers   VARCHAR,
    vendor_hits     VARCHAR
);

-- V1: per-week per-area groupings (no cross-week persistence yet)
CREATE TABLE week_groups (
    week_id           VARCHAR NOT NULL,
    area              VARCHAR NOT NULL,
    group_key         VARCHAR NOT NULL,   -- "kb:audio:KB5036980" etc.
    canonical_item_id VARCHAR NOT NULL,
    member_count      INT,
    PRIMARY KEY (week_id, area, group_key)
);

CREATE TABLE week_group_members (
    week_id       VARCHAR NOT NULL,
    area          VARCHAR NOT NULL,
    group_key     VARCHAR NOT NULL,
    item_id       VARCHAR NOT NULL,
    is_canonical  BOOLEAN,
    PRIMARY KEY (week_id, area, group_key, item_id)
);

-- Per-item score
CREATE TABLE scores (
    item_id       VARCHAR PRIMARY KEY,
    score         DOUBLE,
    engagement_w  DOUBLE,
    source_w      DOUBLE,
    recency_w     DOUBLE,
    computed_at   TIMESTAMP
);

-- Weekly area rollups (includes taxonomy version for trend continuity)
CREATE TABLE weekly_rollup (
    week_id              VARCHAR NOT NULL,
    area                 VARCHAR NOT NULL,
    taxonomy_version     VARCHAR NOT NULL,  -- hash of taxonomy.yaml at run time
    item_count           INT,
    bug_count            INT,
    feature_request_count INT,
    feedback_count       INT,
    praise_count         INT,
    workaround_count     INT,
    avg_sentiment        DOUBLE,
    weighted_sentiment   DOUBLE,
    severity_max         VARCHAR,
    group_count          INT,
    top_group_keys_json  VARCHAR,
    top_vendors_json     VARCHAR,
    computed_at          TIMESTAMP,
    PRIMARY KEY (week_id, area)
);

-- Run log with completeness metrics
CREATE TABLE runs (
    run_id           VARCHAR PRIMARY KEY,
    week_id          VARCHAR,
    started_at       TIMESTAMP,
    finished_at      TIMESTAMP,
    status           VARCHAR,
    stage_durations  VARCHAR,         -- JSON
    counters         VARCHAR,         -- JSON (fetched, dropped, relevant, classified, failed)
    completeness     VARCHAR,         -- JSON (ceiling_hits, comment_truncations, etc.)
    errors           VARCHAR,
    taxonomy_version VARCHAR,
    vendors_version  VARCHAR,
    code_version     VARCHAR          -- git commit hash
);
```

V1.5 adds: `issues`, `issue_members`, `issue_state_transitions`.
V2 adds: `embeddings.parquet` + an additional `issue_match_method` column on `issue_members` (deterministic vs embedding).

### 5.3 State (SQLite)

`seen_ids`, `cursors`. Append-only.

### 5.4 Idempotency Honest Statement

| Stage | Reproducible from raw? | Notes |
|---|---|---|
| Fetch | N/A (source of truth) | |
| Normalize | Yes | Deterministic transform of JSONL. |
| Filter | Yes | Pure function of items + filter config. |
| Relevance (LLM) | **No** | Same input may yield different outputs. Mitigation: `temperature=0`, `seed=fixed`. Drift still possible. |
| Classify (LLM) | **No** | Same. |
| Group | Yes | Deterministic function of (items, classifications, regex_extractions). |
| Score | Yes | |
| Aggregate | Yes | |
| Render | Yes | |

The system is **idempotent for the same LLM outputs**. Re-running classification will likely produce slightly different outputs, which will produce slightly different rollups. Raw JSONL is the only true rebuild source; the warehouse is best-effort derived.

V1.5+ adds an `is_locked` flag on past weeks: re-running on a locked week is forbidden. Only the current week is writable. This makes issue identity (which depends on past weeks' group keys) stable.

---

## 6. Configuration (Hand-edited YAML in V1)

### 6.1 `sources.yaml`

```yaml
sources:
  - id: reddit
    type: reddit
    credibility_weight: 1.0
    streams:
      - subreddit: Windows11
        display: r/Windows11
        engagement_threshold: 5
      - subreddit: Windows
        display: r/Windows
        engagement_threshold: 5
      # ... etc.
```

### 6.2 `taxonomy.yaml`

```yaml
version: "2026-05-29"   # required, used in weekly_rollup
areas:
  - id: audio
    display: Audio
    enabled: true
    keywords: [audio, sound, speaker, bluetooth audio, headphone]
  - id: camera
    display: Camera
    enabled: true
    keywords: [camera, webcam]
  # ...
```

Editing `taxonomy.yaml` requires bumping `version`. Trend charts (in V2.5+) render a discontinuity marker where taxonomy_version changes between weeks.

### 6.3 `vendors.yaml`

```yaml
version: "2026-05-29"
types: [gpu, cpu, laptop, desktop, driver, audio_technology, microphone, ...]
vendors:
  - canonical: Dolby
    aliases: [dolby labs, dolby laboratories]
    types: [audio_technology]
    products: [Atmos, Vision, AC-4]
    active: true
  - canonical: Intel
    aliases: [intel corp]
    types: [cpu, driver, network_adapter, gpu]
    products: [AX211, AX210, Arc, Iris Xe, Core]
    active: true
```

### 6.4 `app.yaml`

```yaml
llm:
  relevance:
    endpoint: http://localhost:5273/v1
    model: phi-4-mini
    temperature: 0
    seed: 42
    timeout_seconds: 20
  classify:
    endpoint: http://localhost:5273/v1
    model: phi-4-mini
    temperature: 0
    seed: 42
    timeout_seconds: 60
    use_guided_decoding: true
    fallback_repair_attempts: 1

paths:
  data_root: ./data
  reports_root: ./reports
  warehouse_db: ./data/warehouse.duckdb
  state_db: ./data/state.sqlite

fetching:
  default_engagement_threshold: 5
  triangulate: true              # union .new + .top + .controversial
  fetch_all_comments: true       # no engagement gating on comment fetch

reporting:
  trend_weeks: 4
  top_items_per_area: 10
```

---

## 7. Eval Plan (built first, before pipeline code)

The single biggest risk in V1 is "Phi-4-mini isn't good enough." We measure that before we build around it.

### 7.1 Golden Set

- 100–200 items hand-pulled from real Reddit threads across all enabled areas.
- Each labeled with: areas, content_types, sentiment, severity (if bug), Windows version (if mentioned), entities, KB numbers.
- Stored as JSONL in `eval/golden_set.jsonl`.
- Versioned; new examples appended over time.

### 7.2 Eval Harness

`eval/run_eval.py`:

- Runs the same classify+extract pipeline against the golden set.
- Computes per-dimension metrics:
  - Areas: precision/recall/F1 (multi-label)
  - Content types: precision/recall/F1 (multi-label)
  - Sentiment: MAE against label, correlation
  - Severity (bugs only): accuracy
  - Windows version: exact-match accuracy on `major`, `feature_update`
  - Entities: precision/recall on `(vendor, product)` pairs
  - KB numbers: precision/recall

- Outputs a JSON report and a human-readable Markdown summary.

### 7.3 Acceptance Gate

V1 ships when:

- F1 ≥ 0.75 on areas, content_types, severity, entities, KB numbers.
- Sentiment MAE ≤ 0.25 on a -1..1 scale.
- Windows version major exact-match ≥ 0.85 (when version is explicit in source).

If Phi-4-mini misses these, we fall back to Phi-4 14B (slower but stronger) or split the classifier into more focused calls.

### 7.4 Sentiment Volume Gating

WoW "biggest sentiment mover" tile (in V2.5+) is suppressed for areas with fewer than 30 items in either compared week. Sentiment on small n is noise.

### 7.5 Regression on Every Change

Any change to prompts, model, or schema runs `eval/run_eval.py`. Drift > 5 percentage points on any metric blocks the change.

---

## 8. Tech Stack (V1)

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.11+ | |
| Reddit | `praw` | |
| HTTP | `httpx` | for any non-praw HTTP |
| Retries | `tenacity` | |
| LLM classify | Foundry Local + Phi-4-mini | with guided decoding |
| Guided decoding | Foundry Local native if available; else `outlines` | |
| LLM client | `openai` SDK pointed at local endpoint | |
| Schema validation | `pydantic` v2 | |
| Warehouse DB | DuckDB | single file |
| State DB | SQLite | single file |
| Data handling | `polars`, `pyarrow` | |
| Templating | Jinja2 (autoescape on) | static rendering only in V1 |
| Charts | inline SVG, no JS chart lib | minimal deps |
| Config | `pyyaml` + Pydantic for schema | |
| Logging | `structlog` | JSON to file |
| Eval | `pydantic` + simple metrics helpers in `eval/` | |

Deferred to later versions:

- Flask, Plotly — V2.5
- HDBSCAN, numpy cosine, embedding model — V2
- JSON Schema for config — V3 (when admin UI lands)
- RSS libraries — V4

### 8.1 Storage Inventory

Three storage technologies in V1, not four:

- **DuckDB** — all analytical data
- **SQLite** — seen_ids, cursors (small append-only state)
- **JSONL files** — raw items (system of record)
- YAML is config, not storage

Embeddings (Parquet) arrive in V2.

---

## 9. Directory Layout (V1)

```
windows-monitor/
├── README.md
├── DESIGN.md
├── requirements.txt
├── run_weekly.bat
├── config/
│   ├── sources.yaml
│   ├── taxonomy.yaml
│   ├── vendors.yaml
│   └── app.yaml
├── data/
│   ├── raw/
│   ├── warehouse.duckdb
│   └── state.sqlite
├── reports/
│   └── 2026-W21/
├── pipeline/
│   ├── run.py
│   ├── fetch.py
│   ├── normalize.py
│   ├── filter.py
│   ├── relevance.py
│   ├── classify.py
│   ├── extract.py            # regex pre-pass
│   ├── group.py              # deterministic grouping
│   ├── score.py
│   ├── aggregate.py
│   ├── render.py
│   ├── llm.py                # Foundry Local client + guided decoding wrapper
│   ├── storage.py
│   └── models.py             # pydantic
├── sources/
│   ├── __init__.py
│   ├── base.py
│   └── reddit.py
├── report_templates/
│   ├── index.html.j2
│   ├── area.html.j2
│   ├── comments.html.j2
│   └── _item_attribution.html.j2
├── eval/
│   ├── golden_set.jsonl
│   ├── run_eval.py
│   └── reports/
├── scripts/
│   ├── init_db.py
│   ├── label_helper.py       # CLI to label a Reddit URL for the golden set
│   └── backup.py
└── tests/
    ├── test_models.py
    ├── test_filter.py
    ├── test_group.py
    └── test_render.py
```

---

## 10. Implementation Plan

### V1 — ~2 weeks

**Day 1–2: Foundation + Eval**
- Repo scaffolding, requirements, venv
- `pipeline/models.py` (Pydantic schemas)
- `scripts/init_db.py` (V1 schema only)
- `scripts/label_helper.py` — interactive CLI for labeling Reddit URLs
- Build initial golden set (~50 items hand-labeled)
- `eval/run_eval.py` skeleton — runs classify against golden set, reports metrics
- **Smoke test:** Phi-4-mini on 10 labeled items. Confirm guided decoding works. If not, fall back to `outlines`.

**Day 3–4: Fetch + Filter**
- Source abstraction (`sources/base.py`)
- Reddit connector with triangulation, full comment fetch, ceiling-hit logging
- Normalize, Filter Stage A
- End-to-end: real subreddit data → JSONL → items table

**Day 5–6: LLM Path**
- `pipeline/relevance.py` — cheap relevance gate
- `pipeline/classify.py` — full classify+extract with guided decoding
- `pipeline/extract.py` — regex pre-pass
- Validate-and-repair fallback path
- **Run eval harness on full pipeline; iterate prompts until acceptance gates pass.** Grow golden set to ~150 items in this phase.

**Day 7–8: Grouping + Aggregation**
- `pipeline/group.py` — deterministic grouping
- `pipeline/score.py`
- `pipeline/aggregate.py` — weekly_rollup with taxonomy_version captured

**Day 9–10: Reporting**
- Jinja templates with autoescape verified
- `pipeline/render.py` — generates `reports/<week_id>/*.html`
- Build-time validator: every item-displaying template uses `_item_attribution.html.j2`
- `run_weekly.bat`
- End-to-end smoke run on real data

**Exit V1:**
- Eval gates pass.
- Real weekly run produces a real report with grouped issues, source links, no silent data loss.
- Hand-edit YAML works; no UI required.

### V1.5 — ~1 week

- `issues` and `issue_members` tables, keyed on deterministic group_key
- `issue_state_transitions` audit log
- Issue lifecycle (active / dormant / resurfaced)
- Append-only assignment + week-locking
- Per-issue HTML page (`issue_<id>.html`)
- Resurface tracking on main report

### V2 — ~1.5 weeks

- `pipeline/embed.py` — bge-small via Foundry Local or ONNX Runtime
- `pipeline/cluster.py` — greedy nearest-pair matching (not HDBSCAN)
- Hybrid: deterministic key first, embedding match for items with no key
- Embeddings on normalized title+body, NOT on LLM summary (fix from feedback)
- `embeddings.parquet`
- Eval: measure how many additional duplicates V2 catches vs V1.5

### V2.5 — ~1.5 weeks

- Flask app, dashboard, report browser
- Global filters (URL-query-param state)
- Resurfaced-issues panel with mini-timeline
- 4-week trend charts (volume-gated)
- System status panel

### V3 — ~2 weeks

- Admin UI for sources/taxonomy/vendors/topics
- JSON Schema validation, atomic writes, version history
- Vendor analytics page
- Taxonomy versioning surfaced in trend UI
- "Run pipeline now" button

### V4+ — per source

- RSS connector + WindowsCentral feed
- HN, Bluesky, GitHub Issues as separate increments
- Validate source abstraction holds without core changes

---

## 11. Operational Considerations

### 11.1 Foundry Local

- Prerequisite, not managed by this project.
- `app.yaml` configures endpoint and model.
- Pre-run health check (`GET /v1/models`).
- Warm-up retry policy: first call after boot may take 30–60s.
- If guided decoding isn't natively supported, the LLM wrapper detects this at startup and falls back to `outlines` running locally.

### 11.2 Secrets

`.env` (gitignored) for Reddit credentials; `python-dotenv` loads.

### 11.3 Error Isolation

Per-stream errors don't abort the run. Each stage logs to `runs.stage_durations`, `runs.counters`, `runs.completeness`. Re-running a failed stage individually is supported in V1; cross-week re-runs are forbidden from V1.5 onward (write-lock past weeks).

### 11.4 Completeness Logging

`runs.completeness` JSON captures:
- `ceiling_hits` — list of `(subreddit, cursor_gap_seconds)` where `.new()` returned 1000 items and the oldest was newer than cursor
- `comment_load_more_truncations` — count
- `classification_failures` — count and item_ids
- `relevance_gate_dropped` — count
- `regex_kb_unique_count`, `regex_vendor_hit_count`

Surfaced in the V2.5 dashboard. In V1, these live in `data/run_logs/<run_id>.json` for manual inspection.

### 11.5 Performance Budget (V1, GPU available)

| Stage | Wall time |
|---|---|
| Fetch + Normalize | 8–18 min |
| Filter | <1 min |
| Relevance LLM (cheap call) | 3–6 min |
| Classify+Extract LLM | 10–20 min (on fewer items thanks to relevance gate) |
| Group + Score + Aggregate | <1 min |
| Render | <1 min |
| **Total** | **22–46 min** |

### 11.6 XSS / Untrusted Content

- Jinja autoescape is on globally (`Environment(autoescape=True)`).
- All user-authored content (title, body, snippet, verbatim) rendered with autoescape.
- `|safe` filter is grepped for in CI; any usage requires explicit justification comment.

### 11.7 Backup

`scripts/backup.py` zips `data/` + `reports/` + `config/` to a dated archive.

---

## 12. Open Questions

1. **Foundry Local guided-decoding support** — verify at install. If absent, route through `outlines`.
2. **Two-call vs three-call classifier** — start with two, split further if eval reveals weakness on entity assignment.
3. **Phi-4-mini vs Phi-4 14B** — start with mini, switch if eval gates fail.
4. **Golden set growth rate** — start at ~150, target 500 by V2. Active-learning loop (low-confidence items get manually labeled) is a V3+ idea.
5. **V1.5 cross-week key stability** — what happens when an item's primary entity changes between runs (re-classification)? Lock classifications on past weeks; only this week can be re-classified.
6. **Long-term `is_relevant=false` storage** — keep all in V1; revisit if disk pressure emerges.

---

## 13. Source Attribution Guarantees

Unchanged from v0.2. Repeating because it's a load-bearing requirement.

- `RawItem.url` is required; connector contract.
- `items.url` is NOT NULL.
- Every item-displaying template includes `_item_attribution.html.j2`.
- Build-time validator scans templates and fails the build if any item rendering omits the partial.
- Direct links open in new tab with `rel="noopener noreferrer"`.

---

## 14. Appendix A — Enterprise-Grade Variant

If this system were being built for an organization rather than a single user, the shape changes substantially. Below is what would differ and what would stay.

### 14.1 What changes fundamentally

**Authentication, authorization, multi-tenancy.** Hosted web app behind SSO (Microsoft Entra ID / Okta), per-user identity throughout, RBAC at the role level (viewer / analyst / admin / auditor), per-tenant data isolation if SaaS. Audit log of every action with actor identity.

**Deployment topology.** Not a Sunday-morning script. Containerized services on Kubernetes:
- Ingestion workers (horizontally scalable, one per source-type)
- LLM serving (dedicated GPU pool — vLLM or TGI, not Foundry Local; or managed inference like Azure OpenAI / AWS Bedrock for vendor-supported SLAs)
- API service (FastAPI or similar)
- Web frontend (Next.js / React)
- Job orchestrator (Temporal, Airflow, or Dagster)
- Object storage (S3/Azure Blob) for raw JSONL and reports
- Managed Postgres or Snowflake/BigQuery for analytics, not DuckDB

**Storage.** Three tiers:
- **Bronze** — raw JSONL in object storage, partitioned by source/year/month/week; immutable, lifecycle-managed
- **Silver** — normalized + classified items in a columnar warehouse (Snowflake/BigQuery/Redshift); partitioned and clustered for query performance
- **Gold** — pre-aggregated rollups, materialized views for dashboards
- Vector database is **mandatory** at scale — Pinecone, Weaviate, Milvus, or pgvector if Postgres-resident. Brute-force numpy doesn't scale past low millions.

**Source coverage.** Many more sources, many more types: Reddit, HN, Twitter/X, Bluesky, Mastodon, GitHub Issues, Stack Overflow, Microsoft Tech Community, Disqus on review sites, YouTube comments via API, Discord servers via partnership, customer support tickets via Zendesk/Salesforce, sales-call transcripts via Gong, product telemetry signals. Each source has its own connector with its own SLA and rate-limit profile.

**LLM strategy.** Not "Phi-4-mini for everything." A tiered routing layer:
- Cheap dedicated relevance/triage model (fine-tuned BERT or DistilBERT — orders of magnitude cheaper than even Phi-4-mini)
- Mid-tier model for routine classification (fine-tuned Phi-3.5 or Llama 3.1 8B)
- Frontier model (GPT-4-class) reserved for edge cases — flagged by low confidence, novel content, or executive-escalated items
- Continuous fine-tuning loop: high-confidence model outputs become training data for the smaller models, distillation pipeline managed via MLflow or Weights & Biases
- Eval rigor: thousands of items in the golden set, per-segment metrics, A/B testing of prompt and model changes with statistical significance gates

**Privacy & compliance.**
- PII detection and redaction in ingestion (names, emails, phone numbers, internal IDs)
- GDPR right-to-erasure pipeline: a user requesting deletion must be removed from raw, silver, gold, vector DB, and any cached LLM training data
- Data residency: EU customer data stays in EU regions
- SOC 2, possibly ISO 27001, possibly FedRAMP if government adjacent
- Encryption at rest (AWS KMS / Azure Key Vault) and in transit everywhere
- Vendor risk assessment for every external API and model provider
- DLP scanning of generated reports before they're surfaced to non-cleared users

**Observability.**
- Distributed tracing (OpenTelemetry) across the entire pipeline
- Metrics: per-stage latency, throughput, error rate, cost per item, model accuracy drift, embedding distribution drift
- Logs: structured, centralized (Datadog, Splunk, Elastic), retained 1+ years
- Alerting: PagerDuty integration, SLOs on freshness ("issues classified within 24 hours of post"), accuracy ("F1 stays above 0.85 against rolling eval set")
- Dashboards for system health, model health, business KPIs

**Quality engineering.**
- Continuous eval against a large versioned golden set, ran in CI on every change
- Shadow-mode model deployment: new model runs alongside old, outputs compared offline before swap
- Champion/challenger framework for prompt and model changes
- Human-in-the-loop labeling tools (Label Studio, Argilla) and a dedicated labeling team
- Inter-annotator agreement measurement for label quality

**Cross-week issue tracking.** Drops the V1 deterministic key approach in favor of a full clustering system from day one — because at enterprise scale, deterministic keys catch a small fraction. The clustering system is more sophisticated:
- Embeddings versioned and migration-aware (when you change embedding model, you need to re-embed and re-cluster carefully)
- Issue identity guaranteed by a stable assignment service with strong-consistency semantics
- "Issue merge" and "issue split" as first-class operations with audit logs
- Human override on grouping decisions, fed back into the model

**Integration surface.**
- Webhooks: outbound alerts on issue lifecycle events
- API: REST + GraphQL for downstream consumption (BI tools, custom dashboards)
- Connectors out: Slack/Teams notifications, Jira/Linear ticket creation, Salesforce case enrichment
- BI integration: ThoughtSpot, Tableau, Power BI semantic layers over Gold tables

**Cost engineering.**
- Per-tenant cost attribution and chargeback
- LLM cost budgets per workflow, with circuit breakers
- Spot instances for batch workloads, reserved for steady-state
- Tiered storage with automatic lifecycle (hot → warm → cold → glacier)
- FinOps as a discipline, not an afterthought

**Productization.**
- Onboarding flow for new tenants
- In-product configuration UI for sources, taxonomy, vendors, alerting rules — far beyond what's in V3 of the personal tool
- Multi-language support if customer base demands (English-only LLM doesn't cut it)
- Customer success function, SLAs, contractual support response times
- Status page (statuspage.io), incident comms playbooks

### 14.2 What stays the same in concept

- Source abstraction (still need pluggable connectors)
- Four-dimension tagging model (the structural framing of the data is right; what differs is rigor)
- Source attribution guarantees (even more critical when output is shown to executives)
- Eval-first discipline (more rigorous, not less)
- Append-only issue identity
- Honest idempotency story (more important when downstream systems depend on outputs)

### 14.3 Team and timeline

A personal V1 is ~2 weeks for one engineer.

An enterprise V1 — credible MVP with one source, one tenant, one LLM tier, basic clustering, basic web UI, SSO — is **4–8 engineers for 6–9 months**:
- 1–2 ML engineers (eval, fine-tuning, model routing)
- 2 backend engineers (ingestion, pipeline, API)
- 1 frontend engineer
- 1 platform/DevOps engineer
- 1 product manager
- Part-time security/compliance, design, data engineering

Production-ready (multi-tenant, multi-source, observable, compliant) is **12–18 months** from a standing start.

### 14.4 Practical takeaway

The personal tool and the enterprise product solve the same conceptual problem but are different software systems. Build the personal V1 with discipline (eval-first, append-only issue identity, honest reproducibility) and you'll have made many of the architectural decisions that are reusable. But don't pre-build for the enterprise version — the constraints are too different, and the wrong abstractions early are worse than no abstractions.

The single most transferable habit between the two: **measure quality before building around the model**. Everything else changes; that doesn't.

---

*End of design document, v0.3.*
