# ADR-0026: Split `webui/app.py` into routers + services

- **Status:** Accepted (in progress)
- **Date:** 2026-09-15
- **Area:** frontend/infra

## Context

`webui/app.py` is 6,067 lines and holds 85 `@app.*` routes. It's the single
largest file in the codebase by ~4×. The cost:

- **Merge conflicts.** Every routine change touches app.py. Two people
  cannot edit it in parallel.
- **Reading friction.** Nobody outside the maintainer's head opens this
  file voluntarily — which for an open-source project is the whole
  point. Contributors who might land a route stop at the file size.
- **Test-blast blindness.** A change in one route's inline helper can
  break routes 3,000 lines away because they share `_grouped_fields`,
  `_APP_YAML`, `_product_data_root`, etc.

Routes already cluster naturally by URL prefix, so the split maps 1:1
onto FastAPI `APIRouter` with no redesign: `/admin/*` (11 routes),
`/connections/*` (14), `/products/{id}/snippets/*` (13),
`/products/{id}/runs/*` (12), product-config routes, wizard, report
server. Each lands comfortably under 500 lines.

Zero test rewrites needed: existing route tests hit URLs, not function
names. Grepping the tests confirmed nothing reaches into `webui.app`
for private symbols — the split is invisible to the test suite.

## Options considered

1. **One-shot mechanical split.** Move all 85 routes in one commit. Fastest
   calendar-wise but a review nightmare: 6,000+ lines moved is impossible
   to review meaningfully, and any subtle behavior change (missed helper
   import, off-by-one on a route decorator) is hidden in the noise.

2. **Incremental split, one router per commit.** Land routers/ + services/
   scaffolding + ONE router in a first commit, then one router per follow-
   up. Existing routes keep working in `app.py` while the migration runs;
   test suite is the safety net.

3. **Leave it.** Cost of the size will keep climbing. Not a real option.

## Decision

**Option 2.** This ADR is the first commit:

- `webui/routers/` — one module per URL prefix, each exports an
  `APIRouter`. Mounted from `app.py` via `app.include_router()`.
- `webui/services/` — pure Python business logic. No FastAPI imports,
  no template rendering. Handlers become thin adapters.
- `webui/app.py` — the app factory. Keeps the routes that haven't been
  migrated yet; those move out in follow-up commits.

### First commit contents

Landed in this ADR's implementation:

- `webui/routers/__init__.py`, `webui/routers/health.py`
- `webui/services/__init__.py`, `webui/services/runs.py`
- `webui/app.py` imports and mounts the health router; the old inline
  `/healthz` handler and its private helpers are deleted.
- The remaining ~84 routes stay in `app.py`. Two module-level helpers
  (`_run_logs_dir`, `_product_data_root`) become thin shims that
  delegate to `webui.services.runs` — this lets both the migrated
  health router and every not-yet-migrated route share one
  implementation without changing every call site preemptively.

### Migration playbook (for follow-up commits)

For each URL prefix — pick one, do the full move, commit. Order by
isolation (fewest cross-route helper references first):

1. Grep all `@app.*` decorators in `webui/app.py` whose paths match your
   prefix. Note the line ranges.
2. Grep every private helper (`_foo`) those handlers call. Distinguish:
   - Helpers used ONLY by handlers you're moving → move them into the
     router module (mark them module-private with `_`).
   - Helpers used by handlers you're moving AND by handlers staying in
     app.py → extract them to `webui/services/<domain>.py` first;
     leave a thin shim in `app.py` that delegates (see
     `_run_logs_dir` / `_product_data_root` for the pattern).
3. Create `webui/routers/<prefix>.py`:
   ```python
   from fastapi import APIRouter
   router = APIRouter()

   @router.get("/prefix/route")
   def handler(...): ...
   ```
4. Mount from `app.py`:
   ```python
   from webui.routers import prefix as _prefix_router
   app.include_router(_prefix_router.router)
   ```
5. Delete the migrated `@app.*` handlers and their moved helpers from
   `app.py`. Leave a one-line breadcrumb comment (see the `/healthz`
   deletion for the shape).
6. Run the test suite. Route tests should pass unchanged — they hit
   URLs, not functions.
7. Commit. One prefix per commit keeps blame usable and review
   tractable.

### Rules for services layer

- Services return plain dicts/dataclasses/domain objects.
- Services never import from `fastapi.*` or `webui.templates.*`.
- Services raise domain exceptions (`FileNotFoundError`,
  `ValueError`); routers translate to HTTP status codes.
- A helper is a service candidate if TWO or more handlers use it. One
  handler + one CLI script also qualifies. One handler alone → keep it
  module-private in the router.

## Rationale

- **Incremental over one-shot.** Each commit is reviewable. If a
  migration breaks a route, we bisect to one commit — not to "the
  split." Merge conflicts stop growing the moment the first prefix
  moves out.
- **Services layer decoupled from FastAPI.** The point isn't fewer files
  — it's letting the CLI, wizard scripts, and future integrations call
  the same code the UI calls. Router-split without service extraction
  would leave the codebase with N files that all `from webui.app import
  _foo`, which is worse than the current single file.
- **Shim pattern for shared helpers.** `_run_logs_dir` and
  `_product_data_root` are used by ~20 not-yet-migrated routes.
  Extracting them to services with a shim in `app.py` means those
  routes keep working today AND the migrated router has a clean import
  — no forced big-bang.

## Consequences

**Easier:**
- Adding a route: create/edit a small router file, not hunt through
  6,000 lines.
- Reviewing changes: PR diffs stay in one router.
- Sharing logic with the CLI (`pipeline.run` post-run hooks,
  scheduler_runtime): import from `webui.services.*` — no HTTP layer
  in the way.
- Parallel work: ~10 files instead of one → 10× fewer merge conflicts.

**Harder:**
- Migration is bounded work with a real deadline of the operator's
  patience. If it stalls halfway, we live with a hybrid layout
  indefinitely — worse than either extreme. Track completion via
  `grep -c "^@app\." webui/app.py` (target: 0).
- One import layer added (routers/health → services/runs → pipeline.*).
  Trivial versus the size problem.

## Progress tracker

Update this section as routers land.

| Prefix | Routes | Status | Router file | Notes |
|--------|--------|--------|-------------|-------|
| `/healthz` | 1 | Done | `webui/routers/health.py` | Landed in this ADR |
| `/admin/*` | 11 | Pending | — | Owns `_grouped_fields`, `_APP_YAML` — likely to spawn `services/admin_config.py` |
| `/connections/*` + `/api/ollama/*` | 14 | Pending | — | Uses `_connection_status`; extract `services/connections.py` |
| `/products/{id}/snippets/*` | 13 | Pending | — | `services/snippets.py` for the calibrate flow |
| `/products/{id}/runs/*` + `/schedule` | 12 | Pending | — | `services/runs.py` gains `list_runs`, `read_run`, `spawn_run` |
| `/products/{id}/edit/*` + core | ~10 | Pending | — | Product CRUD — likely `services/products.py` |
| `/products/{id}/prompts/*` + suggestions | ~4 | Pending | — | `services/prompts.py` |
| `/products/{id}/report` + taxonomy + sources | ~6 | Pending | — | May split further |
| `/products/{id}/reports/{week}/*` | 3 | Pending | — | Static report server — likely trivial |
| `/products/{id}/items*` | 2 | Pending | — | Small |
| Wizard flow (`/products/create/wizard/*`) | 6 | Pending | — | Merge with existing `webui/wizard.py` (1,843 lines) |
| Home + misc | ~4 | Pending | — | `/`, `/api/refresh`, `/products` create |

Estimated total time to complete: 8-10 hours across ~10 commits.

## Revisit if

- The playbook produces subtle behavior changes (e.g., decorator order
  matters somewhere). Then we roll the offending commit back, not the
  whole plan.
- A prefix genuinely has 3,000+ lines of handler body (unlikely given
  the current distribution). Then that router gets a further split by
  action rather than URL — `admin_prompts.py`, `admin_tuning.py`, etc.
- FastAPI ships an app-factory pattern that changes the mount shape.
  Then this ADR's mechanics update; the goal is unchanged.
