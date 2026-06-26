# V1 Implementation Plan — Customer Feedback Monitor (POC)

**Target:** Proof of concept in ~6 working days.
**Companion to:** `DESIGN.md` v0.3.

## What V1 is — and isn't

V1 is a **proof of concept built on correct foundations**. The goal is to demonstrate the architecture works end-to-end with real Reddit data and produce a real (if imperfect) HTML report. The classifier doesn't need to be *good* yet — it needs to be *correctly wired*. Quality work is V2.

What "correct foundations" means here: the parts of the system that are painful to rework later must be right from day one. Those are:

1. **Source abstraction** — `Source` ABC + `RawItem` contract + cursor handling. Reddit is one implementation, not the only one.
2. **Schema shape (v0.3)** — per-item vs per-area split, `entity_mentions` PK with `type` included, raw JSONL as system of record.
3. **Source attribution** — `RawItem.url` required, `items.url NOT NULL`, shared Jinja attribution partial, build-time validator that fails if any item-displaying template skips it.
4. **Idempotent stage boundaries** — fetch writes JSONL, normalize reads JSONL writes DB, classify reads DB writes DB. Each stage one job, re-runnable on its own.
5. **Guided/structured decoding** in the LLM client interface — even at POC quality, raw JSON from a small model is too unreliable to skip this, and bolting it on later means redoing the client.
6. **Pydantic-typed config** loaded from YAML — not full JSON Schema yet (V3), but typed from day one.
7. **Run log with completeness counters** — counts of what got fetched, dropped, classified, failed. The audit trail.

What V1 explicitly skips:

- Eval harness, golden set, prompt iteration → V2
- Triangulated Reddit fetch (`.top()` + `.controversial()`) → V2
- Two-call classifier with cheap relevance gate → V2
- Comment trees beyond top-level → V2
- Deterministic grouping by `(area, primary_entity)` and title-simhash → V2
- Cross-week issue persistence, resurface tracking, per-issue pages → V1.5
- Validate-and-repair retry on bad LLM output → V2
- Most rollup/scoring complexity. V1 has a flat per-area report.

## Phase summary

| Phase | Duration | Ends with |
|---|---|---|
| 0. Foundation | ~2 days | Repo, config, schema, LLM client with guided decoding, Reddit connector — all present, none doing domain work yet. |
| 1. Pipeline + storage | ~2 days | Real Reddit data flowing fetch → normalize → classify+extract → minimal grouping. Persisted. |
| 2. Report + polish | ~2 days | Real HTML report with full attribution. Build-time validator. Smoke run. README. |

Each phase ends with something runnable that you can demo.

---

# Phase 0 — Foundation (~2 days)

This phase is about getting the load-bearing architecture in place. No domain logic, no LLM calls yet — just structure that the later phases plug into.

## Day 1 — Repo, config, schema

### 0.1 Repo scaffolding

- [ ] Directory layout matching `DESIGN.md §9`. Create every directory, leave files empty for now.
- [ ] `requirements.txt`:
  ```
  praw>=7.7
  httpx>=0.27
  tenacity>=8.2
  openai>=1.40
  pydantic>=2.5
  duckdb>=1.0
  polars>=1.0
  pyarrow>=15.0
  pyyaml>=6.0
  jinja2>=3.1
  structlog>=24.1
  python-dotenv>=1.0
  outlines>=0.0.40
  ```
- [ ] `python -m venv .venv && pip install -r requirements.txt` succeeds.
- [ ] `.gitignore` for `.venv/`, `data/`, `reports/`, `.env`, `__pycache__/`, `*.pyc`.
- [ ] `.env.example` with `REDDIT_CLIENT_ID=`, `REDDIT_CLIENT_SECRET=`, `REDDIT_USER_AGENT=windows-monitor/0.1 by yourname`.
- [ ] `README.md` placeholder — one-paragraph description, link to `DESIGN.md`.

### 0.2 Config files + Pydantic loader

- [ ] `config/app.yaml` from `DESIGN.md §6.4`. Set Foundry Local endpoint to your install.
- [ ] `config/sources.yaml` — three subreddits to start: `Windows11`, `Windows`, `WindowsHelp`. Engagement threshold 5.
- [ ] `config/taxonomy.yaml` — start narrow: `audio`, `camera`, `photos_app`, `drivers`, `graphics`, `copilot`. Each with `version`, `display`, `enabled: true`, short keyword list. **Resist the urge to over-design the taxonomy.**
- [ ] `config/vendors.yaml` — 10–15 seed vendors: Intel, AMD, NVIDIA, Realtek, Microsoft, Dolby, HP, Dell, Lenovo, Logitech, Sony, ASUS. Canonical name + 2–3 products each.
- [ ] `pipeline/config.py` — Pydantic models for each config file (`AppConfig`, `SourcesConfig`, `TaxonomyConfig`, `VendorsConfig`). Single `load_configs()` function that reads all four, validates, returns a typed `Configs` object.
- [ ] Test: introduce a deliberate typo in `app.yaml`, run `load_configs()`, confirm it fails with a useful Pydantic error.

### 0.3 Full schema + DB init

- [ ] `pipeline/models.py` — Pydantic v2 models exactly as in `DESIGN.md §4.6`: `RawItem` (dataclass), `Entity`, `Classification`. Note `Classification` is the full schema including bug/request/context/entities — even though V1's classifier won't fill all fields well, the schema commits to the shape.
- [ ] `scripts/init_db.py` — creates `data/warehouse.duckdb` with the V1 tables from `DESIGN.md §5.2`:
  - `items`
  - `item_classifications`
  - `item_areas`
  - `bug_attributes`
  - `request_attributes`
  - `item_context`
  - `entity_mentions` (PK includes `type`)
  - `regex_extractions`
  - `week_groups`
  - `week_group_members`
  - `runs`
- [ ] Also creates `data/state.sqlite` with `seen_ids(source, external_id, fetched_at)` and `cursors(source, stream, cursor_value, updated_at)`.
- [ ] Idempotent: running twice doesn't error.
- [ ] `pipeline/storage.py` — single module with DuckDB connection helper, SQLite connection helper, and a small set of upsert/select utilities. Don't make it an ORM.

### Day 1 checkpoint

- [ ] `python scripts/init_db.py` produces `warehouse.duckdb` and `state.sqlite`.
- [ ] DuckDB CLI: `.schema` lists all 11 tables. Spot-check that `item_classifications` is keyed only by `item_id`, and `entity_mentions` PK includes `type`.
- [ ] `python -c "from pipeline.config import load_configs; print(load_configs())"` prints a typed config object.

## Day 2 — LLM client, source abstraction, Reddit connector

### 0.4 LLM client with guided decoding

- [ ] `pipeline/llm.py` — wraps the `openai` SDK pointed at Foundry Local. Two functions:
  - `call_text(messages, *, temperature=0, seed=42, timeout=60) -> str` — for unstructured calls (debugging, smoke tests).
  - `call_structured(messages, schema: type[BaseModel], *, temperature=0, seed=42, timeout=60) -> BaseModel` — schema-constrained call.
- [ ] Implementation strategy for `call_structured()`:
  1. **Try Foundry Local native JSON-schema-constrained mode first.** OpenAI-compatible servers usually accept `response_format={"type": "json_schema", "json_schema": ...}`. Pass the Pydantic model's `.model_json_schema()`.
  2. **If that's not supported,** route through `outlines` with the local model. Outlines wraps the model and forces output to conform to the schema by masking tokens at generation time.
  3. **If neither path works,** fail loud at startup. Don't silently fall back to free-form JSON parsing in V1 — the architecture commits to constrained generation, so a missing implementation is a real bug, not a soft problem.
- [ ] Tenacity retry: 3 attempts on timeout / connection error, exponential backoff. No retry on schema validation errors (those are model problems, not infrastructure).
- [ ] `scripts/smoke_llm.py` — two tests:
  1. Calls `call_text()` with `"Say hello"`, prints response.
  2. Calls `call_structured()` with a tiny test schema (`class Greeting(BaseModel): message: str; mood: Literal["happy","sad","neutral"]`), prints a validated object.
- [ ] Both succeed within 60s.

**Trap:** Foundry Local's first call after boot can take 30–60s for model warm-up. The retry policy handles this, but don't conflate "slow first call" with "broken setup."

### 0.5 Source abstraction

- [ ] `sources/base.py`:
  ```python
  from abc import ABC, abstractmethod
  from typing import Iterator
  from pipeline.models import RawItem

  class SourceCursor:
      """Opaque per-source cursor state. Sources interpret the value."""
      source: str
      stream: str          # e.g., subreddit name
      value: str | None    # last-seen marker

  class Source(ABC):
      name: str

      @abstractmethod
      def fetch_since(self, cursor: SourceCursor, config: dict) -> Iterator[RawItem]:
          """Yield items newer than cursor. Must populate RawItem.url with a direct deep link."""
  ```
- [ ] `sources/__init__.py` — registry: `SOURCES = {"reddit": RedditSource}`.

### 0.6 Reddit connector (minimal, but properly abstracted)

- [ ] `sources/reddit.py` — `RedditSource(Source)`:
  - In `__init__`, loads Reddit credentials from `.env`, instantiates `praw.Reddit`.
  - `fetch_since(cursor, config)` iterates `subreddit.new(limit=200)` for the configured subreddit, yields each as a `RawItem`.
  - Filters out items older than `cursor.value` (a Reddit `created_utc` timestamp).
  - For posts above engagement threshold, fetches top-level comments only (no deeper trees in V1) via `submission.comments.replace_more(limit=0)`, yields each as a `RawItem` with `parent_external_id` set.
  - URL construction: `https://reddit.com{permalink}` for both posts and comments.
  - Updates cursor at end of stream to the newest item's `created_utc`.
- [ ] **V2 will add:** triangulation with `.top()` and `.controversial()`, full comment trees, ceiling-hit detection. The connector design accommodates this — the public interface is just `fetch_since()`.
- [ ] `scripts/smoke_reddit.py` — instantiates `RedditSource`, calls `fetch_since(empty_cursor, {"subreddit": "Windows11"})`, prints the first 5 items' titles and URLs. Click a URL — does it go to the right Reddit post?

### Day 2 checkpoint

- [ ] LLM client works with structured output (smoke test passes).
- [ ] Reddit connector pulls real items with correct URLs.
- [ ] Both modules are reachable from the `pipeline/` package — wiring is in place for Phase 1 to connect them.

---

# Phase 1 — Pipeline + Storage (~2 days)

Now wire fetch → normalize → classify → group. Real data, persisted, every stage isolated.

## Day 3 — Fetch, normalize, regex extraction

### 1.1 Fetch orchestrator

- [ ] `pipeline/fetch.py` — single function `run_fetch(configs, run_id) -> FetchResult`:
  - For each enabled source in `sources.yaml`, instantiate the connector.
  - For each stream (subreddit), load cursor from `state.sqlite`, call `source.fetch_since(cursor, stream_config)`.
  - For each yielded `RawItem`: check `seen_ids`; if new, append to `data/raw/<source>/<week_id>/<stream>.jsonl`, insert into `seen_ids`.
  - At stream end, update cursor.
  - Errors on one stream don't stop other streams. Capture in `FetchResult.errors`.
  - Returns counts: total fetched, new, deduped.
- [ ] JSONL is the system of record. Every item that comes out of any source lands here before touching the warehouse.

### 1.2 Normalize

- [ ] `pipeline/normalize.py` — reads JSONL for the current week, upserts rows into `items`:
  - `id = f"{source}:{external_id}"`
  - `week_id` computed from `created_at` (ISO week, e.g., `2026-W21`)
  - `filter_status` left null at this stage
  - All other columns populated from `RawItem`
- [ ] Upsert semantics: same `(source, external_id)` updates instead of erroring.
- [ ] Smoke: run fetch + normalize. `SELECT count(*) FROM items WHERE week_id='2026-W21'` returns a number > 0.

### 1.3 Filter (heuristic only, V1 minimum)

- [ ] `pipeline/filter.py` — applies Stage A from `DESIGN.md §4.4`:
  - Drop body < 50 chars when title isn't informative
  - Drop `[deleted]`, `[removed]`
  - Drop where engagement is below stream threshold AND no watchlist regex match
- [ ] **Filtered items are NOT deleted from `items`.** Set `filter_status = 'kept' | 'dropped_short' | 'dropped_deleted' | 'dropped_low_engagement'`. Audit trail preserved.
- [ ] V1 watchlist regex: KB numbers (`KB\d{7}`), CVE IDs. (Full feature-name watchlist is V2.)

### 1.4 Regex pre-pass

- [ ] `pipeline/extract.py` — for each item with `filter_status='kept'`:
  - Regex-extract KB numbers, CVE IDs, Windows build numbers (`\d{5}\.\d+`)
  - Regex-match vendor names and product names from `vendors.yaml` (canonical + aliases)
  - Persist to `regex_extractions` table
- [ ] This runs *before* classification — the LLM call uses these as hints in the prompt.

### Day 3 checkpoint

- [ ] One real subreddit, 100–300 items fetched, normalized, filtered.
- [ ] `items.filter_status` populated for every row.
- [ ] `regex_extractions` populated for kept items.

## Day 4 — Classify, group, score

### 1.5 Classification (single call, guided decoding)

- [ ] `pipeline/classify.py` — for each kept item:
  - Build the prompt from `DESIGN.md §4.4.6` template, plus regex hints from `regex_extractions`.
  - Call `llm.call_structured(messages, Classification)`.
  - On `is_windows_relevant=false`: persist that fact, skip downstream extraction storage.
  - On success: write to `item_classifications`, `item_areas`, `bug_attributes` (if applicable), `request_attributes` (if applicable), `item_context`, `entity_mentions`.
  - On any unrecoverable error (schema validation fail, timeout after retries): log to `runs.counters.classification_failed`, continue.
- [ ] **V1's single-call posture is intentional.** V2 will split into a cheap relevance gate + full extract for cost reasons. V1 doesn't care about cost.
- [ ] **Don't iterate on the prompt yet.** Use a reasonable first draft. Quality is V2's job.

### 1.6 Grouping (minimum viable)

- [ ] `pipeline/group.py` — for each (week, area):
  - For each item, compute `group_keys`:
    - `kb:{area}:{kb_number}` for every KB number in `regex_extractions`
    - `singleton:{item_id}` if no KB number (every other item is its own group)
  - Items sharing a key form a group.
  - Canonical item per group = highest item_score (V1 uses just engagement, no recency decay).
  - Persist to `week_groups`, `week_group_members`.
- [ ] **V1 grouping is intentionally limited** to KB-based deduplication. Two reports of "audio cuts out after KB5036980" correctly become one group; everything else is a singleton. V2 adds primary-entity grouping and title-simhash fallback.
- [ ] Why bother with grouping at all in a POC? Because (a) it's nearly free given regex extraction exists, (b) the grouping interface needs to be present so V2 can extend it without restructuring, (c) reports without any grouping misrepresent the value prop — even the simplest demonstration that KB-grouped duplicates collapse correctly is worth a lot.

### 1.7 Orchestrator + run log

- [ ] `pipeline/run.py` — composes everything:
  ```python
  def run(week_id: str | None = None):
      run_id = uuid4()
      configs = load_configs()
      with run_log(run_id, week_id) as run:
          run.stage("fetch",     lambda: fetch.run_fetch(configs, run_id))
          run.stage("normalize", lambda: normalize.run(configs))
          run.stage("filter",    lambda: filter_.run(configs))
          run.stage("extract",   lambda: extract.run(configs))
          run.stage("classify",  lambda: classify.run(configs))
          run.stage("group",     lambda: group.run(configs))
          run.stage("render",    lambda: render.run(configs))   # Phase 2
  ```
- [ ] `run_log()` context manager writes an entry to `runs` table with start/end timestamps, stage durations, counters (fetched, dropped, classified, failed), and any errors.
- [ ] Print a summary to stdout at end: how many items in, how many classified, how many groups, total time.

### Day 4 checkpoint

- [ ] Full pipeline runs end-to-end on a real subreddit week (one source for now).
- [ ] `SELECT * FROM runs WHERE run_id='...'` shows complete stage durations and counters.
- [ ] `SELECT count(*) FROM week_groups` returns a sensible number — most singletons, some KB-grouped clusters.
- [ ] Spot-check 10 random items: does the classification look directionally right? (You're not iterating; you're confirming the wiring works.)

---

# Phase 2 — Report + Polish (~2 days)

A real HTML report with full source attribution and a build-time validator that enforces it.

## Day 5 — Templates + renderer

### 2.1 Attribution partial (the load-bearing piece)

- [ ] `report_templates/_item_attribution.html.j2`:
  ```jinja
  <span class="item-attribution">
    <span class="src">{{ item.source_display_name }}</span>
    {% if item.author %}· <span class="author">@{{ item.author }}</span>{% endif %}
    · <span class="when">{{ item.created_at | relative_time }}</span>
    · <a href="{{ item.url }}" target="_blank" rel="noopener noreferrer" class="src-link">↗</a>
  </span>
  ```
- [ ] Every template displaying an item must `{% include '_item_attribution.html.j2' %}` per item. This is the contract.

### 2.2 Templates

- [ ] `report_templates/base.html.j2` — layout with inline CSS. Pull aesthetic from the dashboard mockup if you like.
- [ ] `report_templates/index.html.j2` — main weekly report:
  - Header: week_id, run timestamp, total items, classified items, group count
  - Per-area cards (grid): area name, item count, sentiment average, top 3 groups (each showing the canonical item with attribution partial)
  - Link to each per-area detail page
- [ ] `report_templates/area.html.j2` — per-area page:
  - Header with area name and stats
  - Groups list ranked by member count, descending
  - Each group: canonical item (full body, attribution), expandable list of duplicates (each with attribution)
  - Singletons listed at the bottom in a flat table
- [ ] `report_templates/comments.html.j2` — drill-down: every item in area, with attribution, sentiment, entities, expandable body.

### 2.3 Renderer

- [ ] `pipeline/render.py` — for the current week:
  - Query DuckDB for everything needed (joins across `items`, `item_classifications`, `item_areas`, `entity_mentions`, `week_groups`, `week_group_members`).
  - Render `reports/<week_id>/index.html`.
  - Render `reports/<week_id>/area_<id>.html` for each area with items this week.
  - Render `reports/<week_id>/comments_<id>.html` for each area.
- [ ] Inline SVG for any sparklines or sentiment dials. No external chart libs in V1.
- [ ] Jinja `Environment(autoescape=True)` globally. **No `|safe` filters anywhere in V1.**
- [ ] Custom Jinja filter `relative_time` (e.g., "3 days ago") and `truncate_words`.

### Day 5 checkpoint

- [ ] Pipeline ends by writing `reports/<week_id>/index.html`.
- [ ] Open it in a browser. Per-area cards visible. Click into an area page. Click an item link. Lands on the real Reddit post.

## Day 6 — Validator, smoke run, README

### 2.4 Attribution validator

- [ ] `scripts/validate_templates.py` — scans every `.j2` file in `report_templates/`:
  - Heuristic: any template containing references to `item.title`, `item.body`, `item.url`, or `{% for item in ... %}` blocks is "item-displaying."
  - For each such template, confirm it includes `_item_attribution.html.j2` (either directly or via base inheritance with the partial in scope).
  - Exit code 1 with a clear error if any template fails. Lists the violating template and the offending line numbers.
- [ ] Hook into `pipeline/render.py` — run validator at the start of `render()`. Fail loud before rendering anything.
- [ ] Test it: delete the `{% include %}` from `area.html.j2`, re-run pipeline, confirm it fails with a useful message. Restore the include.

### 2.5 XSS sanity check

- [ ] In `seen_ids`, mark a real item to be re-fetched. Manually edit its title in the `items` table to `<script>alert('xss')</script>`. Re-render.
- [ ] Open the report. The text should display literally, not execute. Confirms Jinja autoescape is doing its job.
- [ ] Revert the title.

### 2.6 Full smoke run

- [ ] `rm -rf data/ reports/` (back up first if you've labeled anything).
- [ ] `python scripts/init_db.py`.
- [ ] `python pipeline/run.py` — full clean run on 3 subreddits.
- [ ] Expected scale: 300–800 items fetched, 60–80% kept after filter, classification completes in 15–30 minutes (V1's single-call posture is slow; V2 fixes).
- [ ] Open the report. Should look credible. Some classifications will be wrong — that's expected, V2 fixes the model quality.

### 2.7 README

- [ ] Quickstart: install, configure `.env`, run `init_db.py`, run `pipeline/run.py`, open report.
- [ ] Troubleshooting: Foundry Local unreachable, Reddit auth, DB locks.
- [ ] Honest description of V1 limits:
  - Classification quality is unmeasured (V2 adds eval)
  - Reddit only (V4+ adds more sources)
  - Top-level comments only (V2 adds deeper trees)
  - KB-only grouping (V2 adds primary-entity + simhash)
  - No web UI (V2.5)
  - Hand-edit YAML (V3 adds admin UI)
- [ ] Pointer to `DESIGN.md` and this plan.

### 2.8 `run_weekly.bat`

- [ ] Two-line batch file: activates venv, runs `python pipeline/run.py`.
- [ ] Smoke: double-click on a Sunday morning, walk away, come back to a fresh report.

### Day 6 checkpoint — V1 POC done

- [ ] Real Reddit data flows through correct architectural seams.
- [ ] Source attribution enforced by build-time validator.
- [ ] Real HTML report with grouped issues and direct source links.
- [ ] All foundational pieces (source abstraction, schema, guided decoding, attribution, run log) in place for V2 to extend without rework.
- [ ] Tag `v1.0-poc` in git.

---

# What V1 deliberately does badly (so V2 has room to improve)

| Area | V1 behavior | V2 fix |
|---|---|---|
| Classifier quality | Unmeasured. No golden set. | Eval harness + 150-item golden set + prompt iteration to F1 ≥ 0.75 |
| Reddit coverage | `.new(limit=200)` only, top-level comments only | Triangulated with `.top()` + `.controversial()`, full comment trees |
| LLM cost | One full call per item, including off-topic noise | Cheap relevance pre-gate, full call only on relevant items |
| Grouping | KB numbers only | Adds primary-entity grouping and title-simhash fallback |
| Error recovery | Log and skip | Validate-and-repair retry for bad LLM output |
| Cross-week | None — each week independent | V1.5 adds issue persistence and resurface tracking |
| Reports | Static HTML, no filters, no interactivity | V2.5 adds Flask + filters + dashboard |

The discipline of V1: build the architecture; resist the urge to also build the quality. The classifier will be mediocre. The report will be honest about being a POC. That's the point.

---

# Cross-cutting practices

- **Commit at the end of every day.** Six tagged commits, six rollback points.
- **Don't add scope mid-flight.** Every "while I'm here, let me also…" is V2's territory.
- **Trust the schema.** It's the v0.3 schema for a reason. Don't simplify it in V1 thinking you'll add columns later — migrations are painful.
- **Run `scripts/validate_templates.py` before every render.** It's hooked into the renderer, so this happens automatically — but resist the urge to disable it during template iteration.
- **Foundry Local is the only external dependency that should ever change in V1.** Don't swap models, don't change endpoints. If something looks broken, the cause is almost always elsewhere.

---

# Daily plan

| Day | Phase | End-of-day state |
|---|---|---|
| 1 | 0.1–0.3 — repo, configs, schema | `init_db.py` works; configs load and validate |
| 2 | 0.4–0.6 — LLM client + Reddit connector | Both smoke tests pass; structured LLM output validated |
| 3 | 1.1–1.4 — fetch, normalize, filter, extract | Real items in DB with filter status and regex extractions |
| 4 | 1.5–1.7 — classify, group, run log | Full pipeline runs end-to-end |
| 5 | 2.1–2.3 — templates + renderer | First real HTML report renders |
| 6 | 2.4–2.8 — validator, smoke run, README | V1.0-poc tagged in git |

Six working days. Slip the right edge if something blows up — never compress the foundation phases. The architecture rework cost in V2 is much higher than a one-day delay in V1.

---

*End of V1 POC implementation plan.*
