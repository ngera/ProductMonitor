"""FastAPI admin app, bound to 127.0.0.1.

Admin tool to manage all products (a.k.a. search topics — Product -> Area
-> Feature hierarchy) and view their generated reports. Routes cover:
product list/create + dashboard, taxonomy / sources / prompts /
llm-routing editors, snippet add/list/edit, run trigger + report viewer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
import json as _json
import re
import shutil
import subprocess
import sys

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import yaml

from pipeline.models import CONTENT_TYPES, SEVERITY_VALUES
from pipeline.snippets import (
    NEGATIVE,
    POSITIVE,
    Snippet,
    delete_snippet,
    load_snippets,
    save_snippet,
    slugify,
)
from pipeline.config import app_config, resolve_path, set_current_product
from datetime import date, datetime, timezone
import uuid
from fastapi import Body

from dotenv import dotenv_values, set_key, unset_key

from pipeline.product import (
    PRODUCTS_DIR,
    VALID_GOALS,
    available_products,
    clear_cache,
    load_product,
    save_product_facts,
    save_product_meta,
    scaffold_product,
)

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"

app = FastAPI(title="ProductMonitor")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- Wizard v2 router (wizard redesign Phase 3) ------------------------------
# Mounted here so /wizard* routes come from webui/wizard.py rather than being
# tangled into this file. Feature-flagged by `wizard_v2_enabled`; when the
# flag is off, every route in the router returns 403.
from webui import wizard as _wizard_v2_router  # noqa: E402
app.include_router(_wizard_v2_router.router)


# --- Scheduler daemon --------------------------------------------------------
# One background thread across the whole webui process fires due pipeline runs
# per product. Idempotency: module-level flag prevents accidental double-start
# on reloads. See webui/scheduler_runtime.py for the tick loop.
_scheduler_started = False

@app.on_event("startup")
def _start_scheduler() -> None:  # pragma: no cover — thread lifecycle
    global _scheduler_started
    if _scheduler_started:
        return
    from webui import scheduler_runtime
    scheduler_runtime.start()
    _scheduler_started = True


# --- Admin: tune pipeline knobs in config/app.yaml --------------------------
#
# A form-based editor over the subset of config/app.yaml that operators
# actually tune day-to-day: filter thresholds, fetch limits, grouping/scoring
# knobs. Anything not in _TUNING_FIELDS (paths, llm, ...) is left untouched
# by the save path.

_APP_YAML = Path(__file__).resolve().parent.parent / "config" / "app.yaml"

# Ordered field spec drives both the form render and the save. Each entry:
#   (section, key, type, default, help, group_label)
_TUNING_FIELDS: list[tuple] = [
    # --- Filter (heuristic drops before the LLM) ---
    ("filter", "min_body_chars", "int", 50,
     "Body character floor. Items shorter than this are dropped as `too_short` "
     "(exceptions: title ≥ 20 chars, or a KB/CVE number hit). "
     "Lower (e.g. 20) keeps more borderline items — useful for niche products with sparse chatter. "
     "Higher (e.g. 100) drops noisy one-liners aggressively — useful on high-volume sources.",
     "Filter"),
    ("fetching", "default_engagement_threshold", "int", 5,
     "Minimum upvotes/comments to survive engagement filtering. "
     "0 keeps everything (recommended for low-traffic support forums like Microsoft Community). "
     "1 drops only fully-unengaged posts. "
     "5 (default) is reasonable for Reddit. "
     "10+ is aggressive — keeps only items the community responded to.",
     "Filter"),
    ("filter", "relevance_drop_confidence", "float", 0.7,
     "The relevance LLM only drops an item when it says 'not relevant' AND is at least this confident. "
     "Higher (0.9) keeps more borderline items — the classifier gets another shot and may catch nuance the relevance gate missed (larger LLM bill). "
     "Lower (0.5) drops more aggressively (smaller bill, risks losing relevant items the gate was uncertain about).",
     "Filter"),
    ("grouping", "simhash_hamming_threshold", "int", 4,
     "Titles within this Hamming distance are treated as duplicates and one is dropped. "
     "0 = only exact matches (very strict). "
     "4 (default) catches most re-wordings of the same complaint. "
     "8+ aggressively merges posts that share half their tokens — useful when users file the same bug in many phrasings.",
     "Filter"),
    # --- Fetching (per-source caps) ---
    ("fetching", "new_limit", "int", 1000,
     "Cap on items pulled from each source's 'new' stream per run. "
     "Higher = more coverage but longer fetch time and more classify tokens. "
     "Lower = faster runs but you may miss items on high-volume sources between runs. "
     "For weekly cadence on Reddit, 1000 is usually enough; on r/all-scale traffic bump to 5000.",
     "Fetching"),
    ("fetching", "top_limit", "int", 100,
     "Cap for the 'top' stream (best-ranked items in the time window). "
     "Lower than `new_limit` because 'top' is high-signal per-item. "
     "Bump for products where the community's rankings matter more than raw recency.",
     "Fetching"),
    ("fetching", "controversial_limit", "int", 50,
     "Cap for the 'controversial' stream (polarizing items). "
     "Controversial posts often surface bugs and design decisions people love-or-hate. "
     "Set to 0 to skip entirely if noise outweighs signal for your product.",
     "Fetching"),
    ("fetching", "max_comments_per_post", "int", 500,
     "Safety cap on comments fetched per post. "
     "Prevents runaway fetch when a post goes viral (10k+ comment threads happen). "
     "500 usually captures the signal; 100 speeds up runs at the cost of missing deep discussion; "
     "2000+ if you're doing deep community analysis.",
     "Fetching"),
    ("fetching", "parent_context_body_chars", "int", 500,
     "How much of a parent post's body is inlined into each comment's classify prompt "
     "so the LLM can understand a bare reply. "
     "More context = better classification of ambiguous replies BUT more tokens per call. "
     "500 chars ≈ 100 tokens; 2000 chars ≈ 400 tokens.",
     "Fetching"),
    ("fetching", "sleep_between_streams_seconds", "int", 2,
     "Politeness delay between hitting a source's different streams. "
     "0 = no wait (only against sources you own or where rate limits aren't an issue). "
     "1-3 is polite for public APIs. "
     "5+ if you keep hitting 429s from a strict host.",
     "Fetching"),
    ("fetching", "triangulate", "bool", True,
     "When on, fetches new + top + controversial and merges (deduplicated). "
     "When off, only 'new' is fetched — faster runs but you miss items that resurfaced from older 'top' rankings. "
     "Turn off for products with fast enough news cycles that 'new' alone captures everything.",
     "Fetching"),
    ("fetching", "fetch_all_comments", "bool", True,
     "When on, comments bypass the engagement filter — every reply under a kept post is fetched. "
     "When off, only high-engagement comments are kept, drastically reducing comment volume "
     "but risking missing important quiet replies (bug repros, workarounds).",
     "Fetching"),
    # --- Grouping / Scoring / Reporting ---
    ("grouping", "feature_implicated_min_confidence", "float", 0.5,
     "Below this confidence, an entity the LLM flagged as 'implicated in the issue' "
     "gets demoted to a weaker role (`hardware_in_use` / `software_in_use`). "
     "Higher (0.7) reduces false blame attributions — safer for public reporting; may miss real culprits the LLM was uncertain about. "
     "Lower (0.3) attributes more aggressively — more noise but catches more real causes.",
     "Grouping"),
    ("scoring", "recency_halflife_days", "int", 7,
     "Item scores decay exponentially with age; this is the half-life. "
     "A 7-day item scores 50% of a fresh one; 14 days = 25%; 21 days = 12.5%. "
     "Lower (2-3) heavily favors this week's chatter — good for fast news cycles and consumer products. "
     "Higher (14-30) keeps older items competitive — good for slow-moving enterprise products where a month-old bug is still worth surfacing.",
     "Scoring"),
    ("reporting", "trend_weeks", "int", 4,
     "Legacy report: how many weeks of history to show in the trend section. "
     "Digest v2 uses its own `digest.trend_buckets` in app.yaml instead — this field only affects pre-digest-v2 reports.",
     "Reporting"),
    ("reporting", "top_items_per_area", "int", 10,
     "Legacy report: max items surfaced per taxonomy area. "
     "Digest v2 uses `headline_top_n` per section instead — this field only affects pre-digest-v2 reports.",
     "Reporting"),
    ("reporting", "top_groups_per_area", "int", 10,
     "Legacy report: max groups surfaced per taxonomy area. "
     "Digest v2 uses persistent-issue clustering instead of taxonomy areas — this field only affects pre-digest-v2 reports.",
     "Reporting"),
]


def _load_app_yaml_raw() -> dict:
    """Fresh (uncached) read of config/app.yaml. app_config() is lru_cached
    and we may have just written to disk."""
    with _APP_YAML.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _current_tuning_values() -> dict:
    """Flat map {(section, key): current_value} for the form."""
    cfg = _load_app_yaml_raw()
    out: dict[tuple[str, str], object] = {}
    for section, key, _t, default, *_ in _TUNING_FIELDS:
        section_dict = cfg.get(section) or {}
        out[(section, key)] = section_dict.get(key, default)
    return out


def _grouped_fields() -> list[tuple[str, list[dict]]]:
    """Return [(group_label, [field_dict, ...])] in _TUNING_FIELDS order."""
    values = _current_tuning_values()
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for section, key, typ, default, help_text, group in _TUNING_FIELDS:
        if group not in groups:
            groups[group] = []
            order.append(group)
        groups[group].append({
            "section": section,
            "key": key,
            "name": f"{section}.{key}",
            "type": typ,
            "value": values[(section, key)],
            "default": default,
            "help": help_text,
        })
    return [(g, groups[g]) for g in order]


def _parse_tuning_form(form: dict) -> tuple[dict, list[str]]:
    """Convert form -> {section: {key: value}}. Returns (updates, errors)."""
    updates: dict[str, dict[str, object]] = {}
    errors: list[str] = []
    for section, key, typ, default, help_text, _g in _TUNING_FIELDS:
        name = f"{section}.{key}"
        raw = form.get(name)
        if typ == "bool":
            val = raw == "on"
        else:
            if raw is None or str(raw).strip() == "":
                errors.append(f"{name}: value is required")
                continue
            try:
                val = int(raw) if typ == "int" else float(raw)
            except ValueError:
                errors.append(f"{name}: expected {typ}, got {raw!r}")
                continue
            if val < 0:
                errors.append(f"{name}: must be ≥ 0")
                continue
        updates.setdefault(section, {})[key] = val
    return updates, errors


@app.get("/admin/tuning", response_class=HTMLResponse)
def admin_tuning(request: Request, saved: int = 0, error: Optional[str] = None):
    return templates.TemplateResponse(
        "admin_tuning.html",
        {
            "request": request,
            "grouped_fields": _grouped_fields(),
            # Repo-relative path so the description reads the same on any
            # developer's laptop or install — the previous str(_APP_YAML)
            # leaked absolute local paths.
            "yaml_path": "config/app.yaml",
            "saved": bool(saved),
            "error": error,
            "admin_active": "tuning",
        },
    )


@app.post("/admin/tuning")
async def admin_tuning_save(request: Request):
    form = dict(await request.form())
    updates, errors = _parse_tuning_form(form)
    if errors:
        msg = " · ".join(errors)[:300]
        return RedirectResponse(url=f"/admin/tuning?error={msg}", status_code=303)

    # Load current YAML, apply the delta, atomic-write, invalidate caches.
    cfg = _load_app_yaml_raw()
    for section, section_updates in updates.items():
        cfg.setdefault(section, {}).update(section_updates)

    tmp = _APP_YAML.with_suffix(_APP_YAML.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False), encoding="utf-8")
    backup = _APP_YAML.with_suffix(_APP_YAML.suffix + ".bak")
    if _APP_YAML.exists():
        _APP_YAML.replace(backup)
    tmp.replace(_APP_YAML)

    # Invalidate the pipeline's lru_cache on app_config so the next run reads
    # the new values.
    from pipeline.config import app_config as _app_cfg
    _app_cfg.cache_clear()
    clear_cache()

    return RedirectResponse(url="/admin/tuning?saved=1", status_code=303)


# --- Admin: feature flags (POST_V1_PLAN §4.13, ADR-0006) --------------------
#
# UI to flip feature flags in config/features.yaml without opening the file.
# Product-level overrides in products/<pid>/features.yaml still work but
# aren't editable here (edit per-product features.yaml directly for those).


_PHASE_ORDER = {
    "trust_plugins_dir": "Phase 1 (foundation)",
    "assistant_llm_enabled": "Phase 2 (LLM contract)",
    "token_monitor_enabled": "Phase 2 (LLM contract)",
    "prompt_caching_enabled": "Phase 2 (LLM contract)",
    "scrapecreators_enabled": "Phase 3 (external sources)",
    "evals_enabled": "Phase 4 (learning loop)",
    "snippet_candidates_enabled": "Phase 4 (learning loop)",
    "snippet_from_review_enabled": "Phase 4 (learning loop)",
    "wizard_enabled": "Phase 5 (guided experience)",
    "prompt_suggestions_enabled": "Phase 5 (guided experience)",
    "rationale_enabled": "Phase 5 (guided experience)",
    "observability_traces_enabled": "Cross-cutting",
}


_FEATURES_YAML = Path(__file__).resolve().parent.parent / "config" / "features.yaml"


@app.get("/admin/prompts", response_class=HTMLResponse)
def admin_prompts(request: Request, saved: Optional[str] = None,
                    reverted: Optional[str] = None, error: Optional[str] = None,
                    note: Optional[str] = None):
    """Master prompt templates — view + edit.

    NOT per-product prompts (those live at /products/<id>/prompts).
    These are the templates that:
      - the scaffold copies into new products' prompts.yaml (`scaffold_*`)
      - the assistant LLM sends during wizard drafting (`assistant_*`)
    Edits are stored in config/prompt_templates.yaml as overrides;
    unchanged templates keep serving the code-level default so upstream
    updates propagate automatically.
    """
    from pipeline import prompt_templates
    rows = prompt_templates.all_current()
    # Group by stage for the UI.
    by_stage: dict[str, list[dict]] = {}
    for r in rows:
        by_stage.setdefault(r["spec"].stage, []).append(r)
    return templates.TemplateResponse(
        "admin_prompts.html",
        {
            "request": request,
            "by_stage": by_stage,
            "admin_active": "prompts",
            "saved": saved,
            "reverted": reverted,
            "error": error,
            "note": note,
        },
    )


@app.post("/admin/prompts")
async def admin_prompts_save(request: Request):
    """Save overrides for changed templates. Textareas that equal the
    code default get dropped from the override file (see save_overrides
    docstring for why)."""
    from pipeline import prompt_templates
    form = await request.form()
    action = (form.get("action") or "save").strip()
    key = (form.get("key") or "").strip()

    if action == "revert" and key:
        try:
            prompt_templates.revert(key)
        except Exception as e:
            return RedirectResponse(
                url=f"/admin/prompts?error=revert+failed:+{str(e)[:80]}",
                status_code=303,
            )
        return RedirectResponse(url=f"/admin/prompts?reverted={key}",
                                status_code=303)

    # Regular save — one textarea per template key.
    values: dict[str, str] = {}
    for k in prompt_templates.TEMPLATES.keys():
        raw = form.get(k)
        if raw is not None:
            values[k] = str(raw)
    try:
        prompt_templates.save_overrides(values)
    except Exception as e:
        return RedirectResponse(
            url=f"/admin/prompts?error=save+failed:+{str(e)[:80]}",
            status_code=303,
        )
    return RedirectResponse(url="/admin/prompts?saved=1", status_code=303)


# --- Prompt templates: JSON import + export ---------------------------------
#
# Exports the current in-effect value of every registered template (defaults
# + overrides applied) as a single JSON file. Import accepts the same shape
# and writes matching keys to the overrides file via save_overrides — unknown
# keys are silently dropped so exports can survive template additions/renames.


PROMPT_EXPORT_VERSION = "1"


@app.get("/admin/prompts/export")
def admin_prompts_export():
    """Download every current template value as a JSON file.

    Format:
        {
          "version": "1",
          "exported_at": "<UTC ISO-8601>",
          "prompts": { "<key>": "<current template text>", ... }
        }
    """
    from pipeline import prompt_templates
    rows = prompt_templates.all_current()
    payload = {
        "version": PROMPT_EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prompts": {r["spec"].key: r["current"] for r in rows},
    }
    body = _json.dumps(payload, indent=2, ensure_ascii=False)
    filename = f"prompt_templates_{datetime.now(timezone.utc).date().isoformat()}.json"
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/prompts/import")
async def admin_prompts_import(file: UploadFile = File(...)):
    """Upload a JSON export produced by /admin/prompts/export and apply it.

    - Unknown keys are silently dropped (a future template rename shouldn't
      break historical exports).
    - Values that match the code default are dropped inside save_overrides,
      keeping the override file tight.
    - Any I/O or parse error round-trips back to the page via ?error=.
    """
    from pipeline import prompt_templates

    try:
        contents = await file.read()
    except Exception as e:
        return RedirectResponse(
            url=f"/admin/prompts?error=upload+failed:+{str(e)[:80]}",
            status_code=303,
        )

    try:
        data = _json.loads(contents.decode("utf-8"))
    except Exception as e:
        return RedirectResponse(
            url=f"/admin/prompts?error=invalid+JSON:+{str(e)[:80]}",
            status_code=303,
        )

    prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(prompts, dict):
        return RedirectResponse(
            url="/admin/prompts?error=import+file+missing+'prompts'+object",
            status_code=303,
        )

    known = set(prompt_templates.TEMPLATES.keys())
    to_save: dict[str, str] = {}
    unknown_keys: list[str] = []
    for key, value in prompts.items():
        if not isinstance(value, str):
            continue
        if key in known:
            to_save[key] = value
        else:
            unknown_keys.append(key)

    if not to_save:
        return RedirectResponse(
            url="/admin/prompts?error=import+contained+no+known+template+keys",
            status_code=303,
        )

    try:
        prompt_templates.save_overrides(to_save)
    except Exception as e:
        return RedirectResponse(
            url=f"/admin/prompts?error=save+failed:+{str(e)[:80]}",
            status_code=303,
        )

    note = f"imported+{len(to_save)}+prompt(s)"
    if unknown_keys:
        note += f"+(skipped+{len(unknown_keys)}+unknown+key(s))"
    return RedirectResponse(
        url=f"/admin/prompts?saved=1&note={note}",
        status_code=303,
    )


@app.get("/admin", response_class=HTMLResponse)
def admin_landing():
    """Admin is a set of tabs — Connections / Tuning / Features. This route
    is the landing entry point from the top-nav; it lands on Connections
    by default because that's where API-key setup lives (the most common
    reason to visit /admin)."""
    return RedirectResponse(url="/connections", status_code=303)


# --- Admin > Tokens tracker (cross-product token usage view) ----------------
#
# Read-only cross-cutting telemetry: total tokens + cost, grouped by
# product / provider / stage / model / role, with a trend chart. Reads
# every product's llm_usage table via `pipeline.token_usage.cross_product_totals`.
# Feature-flagged by `admin_token_tracker_enabled` (defaults ON — ADR-0020,
# read-only admin-only telemetry surface).

_ADMIN_TOKENS_WINDOWS = {
    "1d":  ("Last 24 hours", 1),
    "7d":  ("Last 7 days",   7),
    "30d": ("Last 30 days",  30),
    "90d": ("Last 90 days",  90),
    "all": ("All time",      3650),
}


def _tokens_window(sel: str) -> tuple:
    """Return (label, since_dt, until_dt, days) for a window key. Falls back
    to the default_window_days from config when the key is unknown."""
    from datetime import datetime as _dt, timedelta, timezone
    from pipeline.config import app_config
    default_days = int(
        ((app_config().get("admin") or {}).get("tokens") or {})
        .get("default_window_days", 7)
    )
    if sel not in _ADMIN_TOKENS_WINDOWS:
        sel = f"{default_days}d" if f"{default_days}d" in _ADMIN_TOKENS_WINDOWS else "7d"
    label, days = _ADMIN_TOKENS_WINDOWS[sel]
    until = _dt.now(timezone.utc)
    since = until - timedelta(days=days)
    return label, since, until, days, sel


def _tokens_bucket_for_days(days: int) -> str:
    """Chart bucket size per the design: daily <= 90 days, else weekly."""
    if days <= 90:
        return "day"
    return "week"


def _tokens_trend_png_b64(
    since, until, bucket: str, group_by_axis: str, filters: dict,
) -> str:
    """Render a stacked-line trend chart and return it as a base64 PNG.
    Empty string when matplotlib is unavailable OR no data."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return ""

    from pipeline import token_usage as _tu
    data = _tu.cross_product_totals(
        since, until,
        group_by=(bucket, group_by_axis),
        filters=filters,
    )
    series = data["series"]
    if not series:
        return ""

    # Build {axis_value: {bucket: tokens}} and ordered bucket list.
    from collections import defaultdict, OrderedDict
    buckets_seen: "OrderedDict[str, None]" = OrderedDict()
    per_axis: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in series:
        b = row[bucket]
        a = row[group_by_axis] or "(unset)"
        buckets_seen[b] = None
        per_axis[a][b] += row["tokens"]
    x = list(buckets_seen.keys())

    fig, ax = plt.subplots(figsize=(9, 3.2), dpi=100)
    # Top-6 axis values by total; roll the rest into "other" so the legend
    # stays readable.
    totals_by_axis = sorted(
        ((axis, sum(vals.values())) for axis, vals in per_axis.items()),
        key=lambda kv: -kv[1],
    )
    top = [a for a, _ in totals_by_axis[:6]]
    rest = [a for a, _ in totals_by_axis[6:]]
    if rest:
        other = defaultdict(int)
        for a in rest:
            for b, v in per_axis[a].items():
                other[b] += v
        per_axis["other"] = other
        top.append("other")
    for axis_value in top:
        y = [per_axis[axis_value].get(b, 0) for b in x]
        ax.plot(x, y, label=axis_value, linewidth=2, marker="o", markersize=3)
    ax.set_ylabel("Tokens")
    ax.tick_params(axis="x", labelrotation=45, labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.legend(loc="upper left", fontsize=8, framealpha=0.9, ncol=3)
    ax.set_ylim(bottom=0)
    fig.tight_layout()

    import io, base64
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@app.get("/admin/tokens", response_class=HTMLResponse)
def admin_tokens(
    request: Request,
    window: str = "7d",
    group_by: str = "product_id",
    product_id: str = "",
    provider: str = "",
    stage: str = "",
    role: str = "",
    model: str = "",
):
    from pipeline import features as _features, token_usage as _tu
    if not _features.enabled("admin_token_tracker_enabled"):
        raise HTTPException(status_code=403, detail="admin_token_tracker_enabled is off")

    label, since, until, days, sel = _tokens_window(window)
    bucket = _tokens_bucket_for_days(days)

    filters: dict[str, str] = {}
    for k, v in (
        ("product_id", product_id), ("provider", provider),
        ("stage", stage), ("role", role), ("model", model),
    ):
        v = (v or "").strip()
        if v:
            filters[k] = v

    # Three canonical breakdowns always shown.
    by_product  = _tu.cross_product_totals(since, until, group_by=("product_id",),  filters=filters)["series"][:10]
    by_provider = _tu.cross_product_totals(since, until, group_by=("provider",),    filters=filters)["series"][:10]
    by_stage    = _tu.cross_product_totals(since, until, group_by=("stage",),       filters=filters)["series"][:10]

    # Summary (unfiltered by group_by so tiles reflect the applied filter set).
    summary = _tu.cross_product_totals(since, until, group_by=("product_id",), filters=filters)

    # Chart, colored by the current group_by axis.
    axis = group_by if group_by in ("product_id", "provider", "stage", "role", "model") else "product_id"
    trend_png_b64 = _tokens_trend_png_b64(since, until, bucket, axis, filters)

    facets = _tu.known_facets(since, until)
    delta_tokens = summary["totals"]["tokens"] - summary["totals"].get("prior_tokens", 0)
    delta_cost = round(
        summary["totals"]["cost_usd"] - summary["totals"].get("prior_cost_usd", 0.0), 4,
    )

    return templates.TemplateResponse(
        "admin_tokens.html",
        {
            "request": request,
            "admin_active": "tokens",
            "window_key": sel,
            "window_label": label,
            "windows": _ADMIN_TOKENS_WINDOWS,
            "bucket": bucket,
            "group_by": axis,
            "filters": filters,
            "facets": facets,
            "summary": summary["totals"],
            "delta_tokens": delta_tokens,
            "delta_cost": delta_cost,
            "trend_png_b64": trend_png_b64,
            "by_product": by_product,
            "by_provider": by_provider,
            "by_stage": by_stage,
            "since": since.isoformat(timespec="seconds"),
            "until": until.isoformat(timespec="seconds"),
        },
    )


@app.get("/admin/tokens.csv")
def admin_tokens_csv(
    window: str = "7d",
    product_id: str = "",
    provider: str = "",
    stage: str = "",
    role: str = "",
    model: str = "",
):
    from pipeline import features as _features, token_usage as _tu
    if not _features.enabled("admin_token_tracker_enabled"):
        raise HTTPException(status_code=403, detail="admin_token_tracker_enabled is off")
    _, since, until, _days, sel = _tokens_window(window)
    filters: dict[str, str] = {}
    for k, v in (
        ("product_id", product_id), ("provider", provider),
        ("stage", stage), ("role", role), ("model", model),
    ):
        v = (v or "").strip()
        if v:
            filters[k] = v
    rows = _tu.raw_rows_for_csv(since, until, filters)

    import csv, io
    buf = io.StringIO()
    writer = csv.writer(buf)
    header = [
        "ts", "product_id", "run_id", "stage", "role", "provider", "model",
        "endpoint", "source_id", "prompt_tokens", "completion_tokens",
        "cached_input_tokens", "total_tokens", "cost_usd",
    ]
    writer.writerow(header)
    for r in rows:
        writer.writerow([r.get(k, "") for k in header])
    filename = f"tokens_{sel}_{since.strftime('%Y%m%d')}_{until.strftime('%Y%m%d')}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/admin/features", response_class=HTMLResponse)
def admin_features(request: Request, saved: int = 0, error: Optional[str] = None):
    """List every declared flag with its current global value + phase label.

    Also surfaces the current assistant-LLM configuration so admins have one
    place to see feature-flag state alongside the connection that powers the
    flagged features (wizard drafting, snippet candidates, prompt suggestions).
    """
    from pipeline import assistant_llm as _al
    from pipeline import features as _features

    all_flags = _features.all_flags()
    grouped: dict[str, list[dict]] = {}
    for name, value in sorted(all_flags.items()):
        phase = _PHASE_ORDER.get(name, "Uncategorized")
        grouped.setdefault(phase, []).append({"name": name, "value": bool(value)})

    cfg = _al.current_config()
    assistant_summary = {
        "configured": cfg is not None,
        "endpoint": cfg.endpoint if cfg else "",
        "model": cfg.model if cfg else "",
        "budget_usd": cfg.budget_usd_per_product_per_month if cfg else 0.0,
        "api_key_env": cfg.api_key_env if cfg else "",
        "enabled": _features.enabled("assistant_llm_enabled"),
    }

    return templates.TemplateResponse(
        "admin_features.html",
        {
            "request": request,
            "grouped": grouped,
            "yaml_path": str(_FEATURES_YAML),
            "saved": bool(saved),
            "error": error,
            "assistant_summary": assistant_summary,
            "admin_active": "features",
        },
    )


@app.post("/admin/features")
async def admin_features_save(request: Request):
    """Save the flag matrix by rewriting config/features.yaml atomically.

    All known flag names are read from the form. Any that appear in the
    form as "on" become true; missing = false (HTML checkboxes only submit
    when checked).
    """
    form = dict(await request.form())
    from pipeline import features as _features

    known_flags = list(_features.all_flags().keys())
    new_map = {flag: (form.get(flag) == "on") for flag in known_flags}

    try:
        current = yaml.safe_load(_FEATURES_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        current = {}
    current["features"] = new_map

    tmp = _FEATURES_YAML.with_suffix(_FEATURES_YAML.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(current, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    if _FEATURES_YAML.exists():
        backup = _FEATURES_YAML.with_suffix(_FEATURES_YAML.suffix + ".bak")
        _FEATURES_YAML.replace(backup)
    tmp.replace(_FEATURES_YAML)

    _features.clear_cache()
    return RedirectResponse(url="/admin/features?saved=1", status_code=303)


# --- Index: list + create product -------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    from pipeline import features as _features
    from pipeline import wizard_v2 as _wv2
    products = []
    for pid in available_products():
        try:
            p = load_product(pid)
            products.append({
                "id": p.id,
                "display": p.display,
                "description": p.description,
                "n_sources": len(p.sources),
                "n_areas": len(p.area_ids()),
                "n_features": sum(len(a.get("features") or []) for a in p.enabled_areas()),
                "n_snippets": len(p.snippets),
            })
        except Exception as e:
            products.append({"id": pid, "display": pid, "error": str(e)})

    # In-flight wizard v2 drafts show alongside real products so a half-set-up
    # product doesn't get forgotten between browser sessions. Each draft
    # renders with a "Draft" badge, the current step, and a Resume link that
    # drops the user right back where they left off.
    _STEP_LABELS = {
        "describe":  "Screen 1 — describe",
        "profile":   "Screen 2 — confirm profile",
        "calibrate": "Screen 3 — calibrate",
        "review":    "Screen 4 — review & run",
    }
    _STEP_PROGRESS = {"describe": 25, "profile": 50, "calibrate": 75, "review": 90}
    drafts_v2 = []
    if _features.enabled("wizard_v2_enabled"):
        for d in _wv2.list_drafts(PRODUCTS_DIR):
            drafts_v2.append({
                "slug": d.slug,
                "display": d.display or d.slug,
                "description": d.description or d.url_or_description or "",
                "step": d.step or "describe",
                "step_label": _STEP_LABELS.get(d.step or "describe", d.step or ""),
                "progress_pct": _STEP_PROGRESS.get(d.step or "describe", 0),
                "updated_at": d.updated_at,
                "n_aliases": len(d.aliases or []),
                "n_sources_suggested": len(d.suggested_sources or []),
                "n_judgments": len((d.calibration or {}).get("judgments") or {}),
            })

    # first_run_solution.md §4.3 — when the only product is the shipped demo
    # (or none exist), show a welcome screen with two cards ("Try the demo"
    # and "Monitor your product"). The wizard has to be enabled for the
    # second card to lead somewhere useful.
    non_demo_products = [p for p in products if p["id"] != "demo"]
    # Phase 7 flag flip (ADR-0014): with wizard v2 on and no non-demo products
    # AND no in-flight drafts, send new users straight into /wizard. If a
    # draft exists we still land on the list so the user can pick it up.
    if not non_demo_products and not drafts_v2 and _features.enabled("wizard_v2_enabled"):
        return RedirectResponse(url="/wizard", status_code=303)
    first_run = not non_demo_products and _features.enabled("wizard_enabled")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "products": products,
            "drafts_v2": drafts_v2,
            "first_run": first_run,
            "has_demo": any(p["id"] == "demo" for p in products),
        },
    )


@app.post("/products")
def create_product(
    product_id: str = Form(...),
    display: str = Form(...),
    description: str = Form(""),
):
    product_id = product_id.strip().lower().replace(" ", "-")
    display = display.strip()
    if not product_id or not display:
        raise HTTPException(status_code=400, detail="product_id and display are required")
    try:
        scaffold_product(product_id, display, description.strip())
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=f"/products/{product_id}", status_code=303)


# --- Guided setup wizard (POST_V1_PLAN §4.3) --------------------------------
#
# Feature-flagged by `wizard_enabled`. Draft state lives at
# products/.wizard_drafts/<slug>.yaml so users can leave and resume.
# LLM-assist steps go through pipeline/wizard_llm.py with a 3-attempt
# regeneration cap per step.


@app.get("/products/create/wizard", response_class=HTMLResponse)
def wizard_landing(request: Request):
    """Wizard entry: list existing drafts + entry-point choices
    (fresh / clone)."""
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        return templates.TemplateResponse(
            "wizard.html",
            {"request": request, "flag_off": True, "step": None,
             "draft": None, "drafts": [], "products": [], "error": None},
        )
    drafts = _wiz.list_drafts(PRODUCTS_DIR)
    return templates.TemplateResponse(
        "wizard.html",
        {"request": request, "flag_off": False, "step": "landing",
         "draft": None, "drafts": drafts,
         "products": available_products(), "error": None},
    )


@app.post("/products/create/wizard/start")
async def wizard_start(request: Request):
    """Create a new draft from step-1 identity form."""
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        raise HTTPException(status_code=403, detail="wizard is disabled")

    form = await request.form()
    display = (form.get("display") or "").strip()
    if not display:
        return RedirectResponse(
            url="/products/create/wizard?error=display+is+required",
            status_code=303,
        )
    slug = _wiz.slugify(form.get("slug") or display)
    if (PRODUCTS_DIR / slug).exists():
        return RedirectResponse(
            url=f"/products/create/wizard?error=product+{slug}+already+exists",
            status_code=303,
        )
    existing = _wiz.load_draft(PRODUCTS_DIR, slug)
    if existing is not None:
        return RedirectResponse(
            url=f"/products/create/wizard/{slug}/identity",
            status_code=303,
        )
    draft = _wiz.WizardDraft(
        slug=slug, display=display,
        description=(form.get("description") or "").strip(),
        industry=(form.get("industry") or "").strip(),
        primary_goal=(form.get("primary_goal") or "").strip(),
        # first_run_solution.md §4.1 — pre-populate keyless-first sources
        # (HN search on the display name) so the user's first run works
        # without touching /connections.
        sources=_wiz.keyless_default_sources(slug, display),
    )
    _wiz.save_draft(PRODUCTS_DIR, draft)
    return RedirectResponse(
        url=f"/products/create/wizard/{slug}/scope",
        status_code=303,
    )


@app.post("/products/create/wizard/clone")
async def wizard_clone(request: Request):
    """Clone an existing product's taxonomy/prompts/snippets
    into a fresh draft."""
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        raise HTTPException(status_code=403, detail="wizard is disabled")
    form = await request.form()
    source = (form.get("source_product") or "").strip()
    display = (form.get("display") or "").strip()
    if not source or not display:
        return RedirectResponse(
            url="/products/create/wizard?error=source+and+display+required",
            status_code=303,
        )
    if not (PRODUCTS_DIR / source).exists():
        return RedirectResponse(
            url=f"/products/create/wizard?error=source+product+{source}+not+found",
            status_code=303,
        )
    slug = _wiz.slugify(form.get("slug") or display)
    if (PRODUCTS_DIR / slug).exists():
        return RedirectResponse(
            url=f"/products/create/wizard?error=slug+{slug}+already+exists",
            status_code=303,
        )
    draft = _wiz.clone_from(
        PRODUCTS_DIR / source, slug=slug, display=display,
        description=(form.get("description") or "").strip(),
    )
    # Seed keyless-first sources on clone too — cloned products keep
    # taxonomy/prompts but sources are inherently per-product (§4.1).
    if not draft.sources:
        draft.sources = _wiz.keyless_default_sources(slug, display)
    _wiz.save_draft(PRODUCTS_DIR, draft)
    return RedirectResponse(
        url=f"/products/create/wizard/{slug}/identity",
        status_code=303,
    )


@app.get("/products/create/wizard/{slug}/{step}", response_class=HTMLResponse)
def wizard_step(request: Request, slug: str, step: str):
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        raise HTTPException(status_code=403, detail="wizard is disabled")
    if not _wiz.is_valid_step(step):
        raise HTTPException(status_code=404, detail=f"unknown step {step!r}")
    draft = _wiz.load_draft(PRODUCTS_DIR, slug)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"no draft for {slug!r}")
    return templates.TemplateResponse(
        "wizard.html",
        {"request": request, "flag_off": False, "step": step,
         "draft": draft, "drafts": [], "products": [],
         "step_ids": _wiz.STEP_IDS, "step_labels": dict(_wiz.WIZARD_STEPS),
         "max_regenerations": _wiz.MAX_REGENERATIONS_PER_STEP, "error": None},
    )


@app.post("/products/create/wizard/{slug}/{step}")
async def wizard_step_save(slug: str, step: str, request: Request):
    """Persist step inputs into the draft and advance to the next step
    (or finalize on step 7)."""
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        raise HTTPException(status_code=403, detail="wizard is disabled")
    if not _wiz.is_valid_step(step):
        raise HTTPException(status_code=404, detail=f"unknown step {step!r}")
    draft = _wiz.load_draft(PRODUCTS_DIR, slug)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"no draft for {slug!r}")
    form = dict(await request.form())
    _apply_wizard_step(draft, step, form)
    _wiz.save_draft(PRODUCTS_DIR, draft)

    if step == "snippets":
        # Finalize: materialize + jump to product dashboard.
        try:
            product_dir = _wiz.materialize(
                draft, scaffold_fn=scaffold_product, products_dir=PRODUCTS_DIR,
            )
        except FileExistsError as e:
            return RedirectResponse(
                url=f"/products/create/wizard/{slug}/{step}?error={str(e)[:200]}",
                status_code=303,
            )
        clear_cache()
        return RedirectResponse(url=f"/products/{draft.slug}", status_code=303)

    nxt = _wiz.next_step(step)
    return RedirectResponse(
        url=f"/products/create/wizard/{slug}/{nxt}",
        status_code=303,
    )


@app.post("/products/create/wizard/{slug}/{step}/regenerate")
async def wizard_regenerate(slug: str, step: str, request: Request):
    """Call the assistant LLM to (re-)populate this step's fields.
    Enforces MAX_REGENERATIONS_PER_STEP (D from review)."""
    from pipeline import features as _features, wizard as _wiz
    if not _features.enabled("wizard_enabled"):
        raise HTTPException(status_code=403, detail="wizard is disabled")
    draft = _wiz.load_draft(PRODUCTS_DIR, slug)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"no draft for {slug!r}")
    if step not in _wiz.LLM_ASSISTED_STEPS:
        raise HTTPException(status_code=400, detail=f"step {step!r} has no LLM assist")
    if not draft.can_regenerate(step):
        return RedirectResponse(
            url=f"/products/create/wizard/{slug}/{step}?error=regeneration+cap+reached+({_wiz.MAX_REGENERATIONS_PER_STEP})",
            status_code=303,
        )

    from pipeline import wizard_llm as _wlm
    error = None
    if step == "scope":
        result = _wlm.suggest_scope(draft.description or draft.display)
        if result: draft.scope_in, draft.scope_out = result.scope_in, result.scope_out
        else: error = "assistant+LLM+unavailable"
    elif step == "taxonomy":
        result = _wlm.suggest_taxonomy(draft.description, draft.scope_in, draft.scope_out)
        if result:
            draft.areas = [a.model_dump() for a in result.areas]
        else: error = "assistant+LLM+unavailable"
    elif step == "prompts":
        result = _wlm.suggest_prompts(draft.description, draft.scope_in, draft.areas)
        if result:
            draft.prompts = {
                "relevance": {
                    "system": result.relevance_system,
                    "template": result.relevance_template,
                    "few_shot": {"enabled": True, "n_positive": 3, "n_negative": 2},
                },
                "classify": {
                    "system": result.classify_system,
                    "template": result.classify_template,
                    "extras_instructions": "",
                    "few_shot": {"enabled": True, "n_positive": 2, "n_negative": 1},
                },
            }
        else: error = "assistant+LLM+unavailable"
    elif step == "snippets":
        result = _wlm.suggest_snippets(draft.description, draft.areas)
        if result:
            draft.snippets = [{
                "polarity": s.polarity,
                "title": s.title,
                "body": s.body,
                "labels": s.labels,
                "notes": "seeded by wizard",
            } for s in result.snippets]
        else: error = "assistant+LLM+unavailable"

    draft.note_regeneration(step)
    _wiz.save_draft(PRODUCTS_DIR, draft)
    url = f"/products/create/wizard/{slug}/{step}"
    if error:
        url += f"?error={error}"
    return RedirectResponse(url=url, status_code=303)


def _apply_wizard_step(draft, step: str, form: dict) -> None:
    """Copy form fields into the draft for the given step. Trivial
    per-field mapping; validation is loose to allow partial saves."""
    if step == "identity":
        draft.display = (form.get("display") or draft.display).strip()
        draft.description = (form.get("description") or draft.description).strip()
        draft.industry = (form.get("industry") or "").strip()
        draft.primary_goal = (form.get("primary_goal") or "").strip()
    elif step == "scope":
        draft.scope_in = (form.get("scope_in") or "").strip()
        draft.scope_out = (form.get("scope_out") or "").strip()
    elif step == "taxonomy":
        # Form uses `areas_json`; if present, we replace areas wholesale.
        raw = form.get("areas_json")
        if raw:
            import json as _j
            try:
                parsed = _j.loads(raw)
                if isinstance(parsed, list):
                    draft.areas = parsed
            except Exception:
                pass
    elif step == "prompts":
        # Prompts saved via 4 free-text fields.
        draft.prompts = {
            "relevance": {
                "system": form.get("relevance_system") or "",
                "template": form.get("relevance_template") or "",
                "few_shot": {"enabled": True, "n_positive": 3, "n_negative": 2},
            },
            "classify": {
                "system": form.get("classify_system") or "",
                "template": form.get("classify_template") or "",
                "extras_instructions": "",
                "few_shot": {"enabled": True, "n_positive": 2, "n_negative": 1},
            },
        }
    elif step == "sources":
        # Free-text summary; user can hydrate real sources on the product's
        # sources page. Wizard doesn't try to be the full sources editor.
        raw = form.get("sources_json")
        if raw:
            import json as _j
            try:
                parsed = _j.loads(raw)
                if isinstance(parsed, list):
                    draft.sources = parsed
            except Exception:
                pass
    elif step == "llm":
        # first_run_solution.md §4.2 — the 3-button LLM chooser. Fields:
        #   llm_choice ∈ {hosted, ollama, skip}
        #   for hosted: llm_provider (anthropic | openai | gemini | azure_openai)
        #                 llm_api_key (written into .env under the provider's env var)
        #   for ollama: llm_endpoint / llm_model (both optional; defaults are fine)
        from pipeline import wizard as _wiz_mod
        choice = (form.get("llm_choice") or "").strip()
        if choice not in _wiz_mod.LLM_CHOICES:
            return  # ignore invalid submissions; keep prior state
        draft.llm_choice = choice
        draft.llm_endpoint = (form.get("llm_endpoint") or "").strip()
        draft.llm_model = (form.get("llm_model") or "").strip()
        draft.llm_provider = (form.get("llm_provider") or "").strip()

        if choice == _wiz_mod.LLM_CHOICE_HOSTED:
            preset = _wiz_mod.PROVIDER_PRESETS.get(draft.llm_provider or "", {})
            if preset:
                if not draft.llm_endpoint:
                    draft.llm_endpoint = preset["endpoint"]
                if not draft.llm_model:
                    draft.llm_model = preset["model"]
                api_key = (form.get("llm_api_key") or "").strip()
                if api_key:
                    # Persist into .env under the provider's env var name so
                    # the pipeline's _resolve_api_key picks it up on the next run.
                    _write_env_var(preset["api_key_env"], api_key)
        elif choice == _wiz_mod.LLM_CHOICE_OLLAMA:
            draft.llm_endpoint = draft.llm_endpoint or _wiz_mod.DEFAULT_OLLAMA_ENDPOINT
            draft.llm_model = draft.llm_model or _wiz_mod.DEFAULT_OLLAMA_MODEL

        # Live health check (skipped for "skip"). Kept optional — a failed
        # check surfaces a message but doesn't block advancing, because
        # users may want to save the key first and troubleshoot separately.
        if choice == _wiz_mod.LLM_CHOICE_SKIP:
            draft.llm_health_ok = None
            draft.llm_health_message = ""
        else:
            ok, msg = _wizard_llm_health_check(draft)
            draft.llm_health_ok = ok
            draft.llm_health_message = msg
    elif step == "snippets":
        # No user-edit path at this step in the minimal cut; regenerate + accept.
        pass


def _write_env_var(env_name: str, value: str) -> None:
    """Persist an API key to .env AND sync os.environ so the running
    process picks it up immediately (no restart required). Delegates to
    pipeline.env_writer.set_var for the write+sync pairing."""
    if not env_name or not value:
        return
    from pipeline import env_writer
    env_writer.set_var(ENV_FILE_PATH, env_name, value)


def _wizard_llm_health_check(draft) -> tuple[bool, str]:
    """Run LLMClient.health_check against the draft's chosen LLM.

    Returns (ok, message). Never raises — a health check is diagnostic, not
    a gate on advancing through the wizard.
    """
    from pipeline import wizard as _wiz_mod
    routing = _wiz_mod.build_llm_routing(draft)
    if not routing:
        return False, "endpoint or model missing"

    # Minimal shim: build an LLMClient with an inline llm_routing that
    # doesn't touch product state. We do this by temporarily setting the
    # app_config's `llm` block, then constructing the client with role
    # 'relevance' (health checks are role-agnostic since they hit
    # chat.completions.create).
    from pipeline.llm import LLMClient
    from pipeline.config import app_config as _app_config

    app = _app_config()
    saved_llm = app.get("llm")
    app["llm"] = routing
    try:
        client = LLMClient("relevance")
        ok = bool(client.health_check())
        return (True, "endpoint reachable") if ok else (
            False, "health check failed — check endpoint URL and API key",
        )
    except Exception as e:
        return False, f"could not initialize client: {e}"
    finally:
        if saved_llm is None:
            app.pop("llm", None)
        else:
            app["llm"] = saved_llm


# --- Per-product dashboard --------------------------------------------------


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product_dashboard(request: Request, product_id: str):
    try:
        p = load_product(product_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    from pipeline import connections as _conn
    globally_paused = _conn.paused_types()
    sources_summary = []
    for src in p.sources:
        streams = src.get("streams", []) or []
        stype = src.get("type") or ""
        product_paused = bool(src.get("paused"))
        connection_paused = stype in globally_paused
        # Distinguish which pause is in effect so the user knows where to fix.
        # Precedence in fetch matches this label order: connection > product.
        if connection_paused and product_paused:
            paused_label = "paused (both)"
        elif connection_paused:
            paused_label = "paused (global)"
        elif product_paused:
            paused_label = "paused (product)"
        else:
            paused_label = ""
        sources_summary.append({
            "id": src.get("id"),
            "type": stype,
            "n_streams": len(streams),
            "paused": bool(paused_label),
            "paused_label": paused_label,
        })

    n_positive = sum(1 for s in p.snippets if s.is_positive)
    n_negative = sum(1 for s in p.snippets if s.is_negative)
    n_holdout = sum(1 for s in p.snippets if s.holdout_eval)
    n_features = sum(len(a.get("features") or []) for a in p.enabled_areas())

    return templates.TemplateResponse(
        "product.html",
        {
            "request": request,
            "product": {
                "id": p.id,
                "display": p.display,
                "description": p.description,
                # `extras_class` is optional as of ADR-0015 — empty string
                # signals "no user-authored extras.py, using the default
                # empty ProductExtras".
                "extras_class": (
                    p.extras_cls.__name__
                    if p.product_meta.get("extras_module") else ""
                ),
                "taxonomy_version": p.taxonomy_version,
                "n_areas": len(p.area_ids()),
                "n_features": n_features,
                "areas_preview": p.area_ids()[:6],
                # Product-facts fields for the new Profile card + link target.
                "url": p.url,
                "aliases": p.aliases,
                "not_to_be_confused_with": p.not_to_be_confused_with,
                "goals": p.goals,
                "competitors": p.competitors,
                "scope_in": p.scope_in,
                "scope_out": p.scope_out,
            },
            "sources_summary": sources_summary,
            "snippet_stats": {
                "total": len(p.snippets),
                "positive": n_positive,
                "negative": n_negative,
                "holdout": n_holdout,
            },
        },
    )


# --- Product metadata editor (Phase 4) --------------------------------------


@app.get("/products/{product_id}/edit/meta", response_class=HTMLResponse)
def product_meta_form(request: Request, product_id: str, error: Optional[str] = None):
    p = _product_or_404(product_id)
    tr = p.time_range or {"mode": "incremental"}
    return templates.TemplateResponse(
        "product_meta_form.html",
        {
            "request": request,
            "product": {
                "id": p.id,
                "display": p.display,
                "description": p.description,
                "schedule": p.product_meta.get("schedule") or "weekly",
                "time_range_mode": tr.get("mode") or "incremental",
                "time_range_from": tr.get("range_from") or "",
                "time_range_to": tr.get("range_to") or "",
            },
            "error": error,
        },
    )


@app.post("/products/{product_id}/edit/meta")
def product_meta_save(
    product_id: str,
    display: str = Form(...),
    description: str = Form(""),
    schedule: str = Form("weekly"),
    time_range_mode: str = Form("incremental"),
    time_range_from: str = Form(""),
    time_range_to: str = Form(""),
):
    _product_or_404(product_id)
    display = display.strip()
    if not display:
        return RedirectResponse(
            url=f"/products/{product_id}/edit/meta?error=Display+name+is+required",
            status_code=303,
        )
    if time_range_mode not in ("incremental", "last_week", "last_month", "range"):
        time_range_mode = "incremental"
    if time_range_mode == "range" and (not time_range_from or not time_range_to):
        return RedirectResponse(
            url=f"/products/{product_id}/edit/meta?error=Range+mode+needs+both+from+and+to+dates",
            status_code=303,
        )
    time_range = {"mode": time_range_mode}
    if time_range_mode == "range":
        time_range["range_from"] = time_range_from
        time_range["range_to"] = time_range_to
    try:
        save_product_meta(product_id, display, description, schedule, time_range=time_range)
    except Exception as e:
        return RedirectResponse(
            url=f"/products/{product_id}/edit/meta?error={str(e)[:120]}",
            status_code=303,
        )
    return RedirectResponse(url=f"/products/{product_id}", status_code=303)


# --- Profile page (wizard redesign Phase 6) ---------------------------------
#
# Structured facts editor for a live product. Same chip UI as the wizard's
# Screen 2 (via the shared `wizard/_chips.html` partial). Independent of the
# wizard flag — always available so users can maintain facts after creation.

def _product_as_wizard_draft(product):
    """Adapter — return a SimpleNamespace shaped like WizardV2Draft, populated
    from an existing product. Lets `webui/templates/wizard/step_profile.html`
    (originally built for wizard drafts) also render as the /products/<id>/
    profile edit page. Fields the wizard template touches:
        slug, display, url, description, aliases, not_to_be_confused_with,
        competitors (list of name strings), scope_in, scope_out, goals,
        regenerations (empty), drafting_error, page_fetch_failed, updated_at
    Rich competitor attributes (aliases/color/context) are preserved on the
    ProductSpec side; the wizard chip UI only edits the visible NAME.
    """
    from types import SimpleNamespace
    comp_names = [
        (c.get("name") if isinstance(c, dict) else str(c)) or ""
        for c in (product.competitors or [])
    ]
    return SimpleNamespace(
        slug=product.id,
        display=product.display or product.id,
        description=product.description or "",
        url=getattr(product, "url", "") or "",
        aliases=list(product.aliases or []),
        not_to_be_confused_with=list(product.not_to_be_confused_with or []),
        competitors=[n for n in comp_names if n],
        scope_in=list(product.scope_in or []),
        scope_out=list(product.scope_out or []),
        goals=list(product.goals or []),
        regenerations={},
        drafting_error="",
        page_fetch_failed=False,
        updated_at="",
    )


@app.get("/products/{product_id}/profile", response_class=HTMLResponse)
def product_profile(request: Request, product_id: str, error: Optional[str] = None,
                    notice: Optional[str] = None):
    p = _product_or_404(product_id)
    return templates.TemplateResponse(
        "wizard/step_profile.html",
        {
            "request": request,
            "draft": _product_as_wizard_draft(p),
            "edit_mode": True,
            "valid_goals": VALID_GOALS,
            "regen_cap": 0,   # regen buttons hidden via edit_mode anyway
            "error": error,
            "notice": notice,
        },
    )


@app.post("/products/{product_id}/profile")
async def product_profile_save(product_id: str, request: Request):
    product = _product_or_404(product_id)
    form = await request.form()

    def _lines(name: str) -> list[str]:
        raw = form.get(name) or ""
        out, seen = [], set()
        for ln in raw.splitlines():
            s = ln.strip()
            if s and s not in seen:
                seen.add(s); out.append(s)
        return out

    # Competitors here are the chip textarea (one name per line) — matches
    # the wizard UX. Preserve rich attributes (aliases/color/context) for
    # names still present by matching case-insensitively against the
    # existing competitor list; new names get defaults; removed names drop.
    existing_by_lower = {
        (c.get("name") if isinstance(c, dict) else str(c) or "").strip().lower(): c
        for c in (product.competitors or [])
        if (c.get("name") if isinstance(c, dict) else str(c) or "").strip()
    }
    competitors: list[dict] = []
    for name in _lines("competitors"):
        key = name.lower()
        prev = existing_by_lower.get(key)
        if isinstance(prev, dict):
            # Preserve prior attrs; refresh the display name in case of casing edit.
            merged = dict(prev)
            merged["name"] = name
            competitors.append(merged)
        else:
            competitors.append({
                "name": name, "aliases": [], "color": None, "context": "",
            })

    facts = {
        "url": (form.get("url") or "").strip(),
        "aliases": _lines("aliases"),
        "not_to_be_confused_with": _lines("not_to_be_confused_with"),
        "goals": [g for g in form.getlist("goals") if g in VALID_GOALS],
        "competitors": competitors,
        "scope_in": _lines("scope_in"),
        "scope_out": _lines("scope_out"),
    }
    try:
        save_product_facts(product_id, facts)
    except ValueError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/profile?error={str(e)[:120]}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/products/{product_id}/profile?notice=saved",
        status_code=303,
    )


# --- Delete product (configuration only) -----------------------------------
#
# Removes the products/<id>/ directory (taxonomy, prompts, sources,
# llm_routing, extras, examples, product.yaml). Reports (reports_root/<id>/),
# run logs (run_logs_root/<id>/), and warehouse data (data_root/<id>/) are
# NOT touched — they remain readable for anyone who still has a bookmarked
# report URL. Deletion is irreversible from the UI; users can rebuild the
# product via the wizard, but the new instance won't share the deleted config.

@app.post("/products/{product_id}/delete")
def product_delete(product_id: str, request: Request,
                   confirm_slug: str = Form(...)):
    """Delete the product's config directory. Requires the caller to type
    the slug back in `confirm_slug` as a safety interlock."""
    # Load first — 404 if the product doesn't exist. Bypass the LRU cache
    # to catch out-of-band deletes.
    clear_cache()
    p = _product_or_404(product_id)
    if confirm_slug.strip() != product_id:
        return RedirectResponse(
            url=f"/products/{product_id}?error=confirmation+did+not+match",
            status_code=303,
        )
    target = p.dir
    # Belt-and-braces: refuse to delete anything outside PRODUCTS_DIR.
    try:
        target.resolve().relative_to(PRODUCTS_DIR.resolve())
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="refusing to delete a path outside PRODUCTS_DIR")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"{target} is not a directory")
    shutil.rmtree(target)
    clear_cache()
    return RedirectResponse(url="/?notice=deleted+" + product_id, status_code=303)


# --- LLM routing form (Phase 8) ---------------------------------------------
#
# Per-stage LLM adapter config: which endpoint, which model, what
# temperature, etc. Each product owns its own routing so different products
# can target different providers.

LLM_PRESETS = [
    {"label": "Foundry Local — Phi-4-mini (Windows local)",
     "endpoint": "http://localhost:5273/v1", "model": "phi-4-mini",
     "note": "Local Foundry Local install. Requires phi-4-mini downloaded via Foundry."},
    {"label": "Ollama — Phi-4-mini (cross-platform local)",
     "endpoint": "http://localhost:11434/v1", "model": "phi4-mini",
     "note": "Local Ollama install (Mac / Linux / Windows). `ollama pull phi4-mini` first."},
    {"label": "Anthropic — Claude Haiku 4.5 (hosted)",
     "endpoint": "https://api.anthropic.com/v1", "model": "claude-haiku-4-5-20251001",
     "note": "Requires ANTHROPIC_API_KEY in .env. Best fit for the cheap relevance stage."},
    {"label": "Anthropic — Claude Sonnet 4.6 (hosted)",
     "endpoint": "https://api.anthropic.com/v1", "model": "claude-sonnet-4-6",
     "note": "Requires ANTHROPIC_API_KEY in .env. Recommended for classify."},
    {"label": "OpenAI — gpt-4o-mini (hosted)",
     "endpoint": "https://api.openai.com/v1", "model": "gpt-4o-mini",
     "note": "Requires OPENAI_API_KEY in .env."},
    {"label": "OpenAI — gpt-4o (hosted)",
     "endpoint": "https://api.openai.com/v1", "model": "gpt-4o",
     "note": "Requires OPENAI_API_KEY in .env."},
]


@app.get("/products/{product_id}/llm_routing", response_class=HTMLResponse)
def llm_routing_form(request: Request, product_id: str, saved: Optional[str] = None, error: Optional[str] = None):
    product = _product_or_404(product_id)
    routing = product.llm_routing or {}
    # Pre-detect provider per stage from the saved endpoint so the form
    # can highlight the right option in the provider dropdown.
    detected = {
        stage: _detect_provider((routing.get(stage) or {}).get("endpoint", ""))
        for stage in ("relevance", "classify")
    }
    return templates.TemplateResponse(
        "llm_routing_form.html",
        {
            "request": request,
            "product": product,
            "routing": routing,
            "providers": _llm_providers_for_template(),
            "detected_provider": detected,
            "saved": saved,
            "error": error,
        },
    )


@app.post("/products/{product_id}/llm_routing")
async def llm_routing_save(product_id: str, request: Request):
    product_dir = _product_dir_for(product_id)
    form = await request.form()

    def _num(name: str, default, kind):
        v = form.get(name)
        if v is None or str(v).strip() == "":
            return default
        try:
            return kind(v)
        except (TypeError, ValueError):
            return default

    def _resolve_endpoint(stage: str) -> str:
        """Derive endpoint from the provider dropdown selection. For
        the 'custom' option, use whatever the user typed in the
        endpoint-override field."""
        provider = (form.get(f"{stage}.provider") or "").strip()
        if provider == "custom":
            return (form.get(f"{stage}.endpoint") or "").strip()
        meta = CONNECTION_META.get(provider) or {}
        if meta.get("category") == "llm":
            return meta.get("api_endpoint") or ""
        # Fallback: provider unknown, honor the (possibly hidden) endpoint field
        return (form.get(f"{stage}.endpoint") or "").strip()

    def _resolve_model(stage: str) -> str:
        """The Model field is now a <select>. The sentinel value
        '__custom__' means "the user wants a model id not in the
        provider's recommended list" — read it from the side text input."""
        m = (form.get(f"{stage}.model") or "").strip()
        if m == "__custom__":
            return (form.get(f"{stage}.model_custom") or "").strip()
        return m

    new_doc = {
        "relevance": {
            "endpoint": _resolve_endpoint("relevance"),
            "model": _resolve_model("relevance"),
            "temperature": _num("relevance.temperature", 0, float),
            "seed": _num("relevance.seed", 42, int),
            "timeout_seconds": _num("relevance.timeout_seconds", 20, int),
            "max_retries": _num("relevance.max_retries", 3, int),
        },
        "classify": {
            "endpoint": _resolve_endpoint("classify"),
            "model": _resolve_model("classify"),
            "temperature": _num("classify.temperature", 0, float),
            "seed": _num("classify.seed", 42, int),
            "timeout_seconds": _num("classify.timeout_seconds", 60, int),
            "max_retries": _num("classify.max_retries", 3, int),
            "use_guided_decoding": form.get("classify.use_guided_decoding") == "on",
            "fallback_repair_attempts": _num("classify.fallback_repair_attempts", 1, int),
        },
    }

    errors = []
    for stage in ("relevance", "classify"):
        if not new_doc[stage]["endpoint"]:
            errors.append(f"{stage}.endpoint is required")
        if not new_doc[stage]["model"]:
            errors.append(f"{stage}.model is required")
    if errors:
        return RedirectResponse(
            url=f"/products/{product_id}/llm_routing?error=" + " | ".join(errors)[:300],
            status_code=303,
        )

    routing_path = product_dir / "llm_routing.yaml"
    backup_path = routing_path.with_suffix(".yaml.bak")
    if routing_path.exists():
        routing_path.replace(backup_path)
    try:
        routing_path.write_text(
            yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        clear_cache()
        load_product(product_id)
    except Exception as e:
        if routing_path.exists():
            routing_path.unlink()
        if backup_path.exists():
            backup_path.replace(routing_path)
        clear_cache()
        return RedirectResponse(
            url=f"/products/{product_id}/llm_routing?error={str(e)[:200]}",
            status_code=303,
        )
    if backup_path.exists():
        backup_path.unlink()
    return RedirectResponse(url=f"/products/{product_id}/llm_routing?saved=1", status_code=303)


# --- Connections (per-source-type connection params, global) ----------------
#
# Connections are credentials / endpoint settings that belong to a source
# TYPE, not to a per-product source instance. They live in the project-root
# .env file (which python-dotenv reads at process start). The connection
# editor reads + writes that file in place via dotenv.set_key, preserving
# any unrelated keys + comments.

ENV_FILE_PATH = Path(__file__).resolve().parent.parent / ".env"

# --- LLM provider metadata (unchanged from V1) ------------------------------
# Sources' connection metadata now comes from each plugin's MANIFEST
# (POST_V1_PLAN §4.1, ADR-0001). LLM providers keep their hardcoded entries
# below until LLMAdapterManifest lands in a follow-on plan.
_LLM_CONNECTION_META: dict[str, dict] = {
    # LLM entries below (skipped for now, filled from the original dict)
}


# --- Source metadata now comes from plugin manifests ------------------------
# The rest of the dict below is left intact for the LLM entries; the source
# entries (reddit through youtube_comments) become unused values that we
# override at the bottom of this section.
_LEGACY_INLINE_META_KEPT_FOR_LLM: dict[str, dict] = {
    # --- Sources (superseded by SourceManifest — see registry-driven build below) ---
    "reddit": {
        "category": "source",
        "display": "Reddit",
        "url": "https://www.reddit.com",
        "help": (
            "Reddit Data API (non-commercial). Register a Script-type app at "
            "https://www.reddit.com/prefs/apps and put the client id + secret "
            "below. Approval can take 2-4 weeks — see "
            "documents/REDDIT_APPROVAL_PLAN.md."
        ),
        "fields": [
            {"env": "REDDIT_CLIENT_ID", "label": "Client ID", "type": "text", "default": "",
             "help": "The short string under the app name (under 'personal use script') on the prefs/apps page."},
            {"env": "REDDIT_CLIENT_SECRET", "label": "Client Secret", "type": "secret", "default": "",
             "help": "The 'secret' field on the app registration. Treated as a credential."},
            {"env": "REDDIT_USER_AGENT", "label": "User Agent", "type": "text",
             "default": "product-monitor:0.1 (by /u/yourname)",
             "help": "Reddit-mandated format: <platform>:<app-id>:<version> (by /u/<username>). Non-conforming UAs are rate-limited or blocked."},
        ],
    },
    "github_issues": {
        "category": "source",
        "display": "GitHub Issues",
        "url": "https://github.com",
        "help": (
            "GitHub REST API for public issue trackers. Create a fine-grained "
            "PAT at https://github.com/settings/tokens?type=beta with "
            "permissions: Public Repositories (read-only). No approval needed."
        ),
        "fields": [
            {"env": "GITHUB_TOKEN", "label": "Personal Access Token (PAT)", "type": "secret", "default": "",
             "help": "Fine-grained PAT, public-repos read access. Starts with 'github_pat_…'. Treated as a credential."},
        ],
    },
    "hn": {
        "category": "source",
        "display": "Hacker News",
        "url": "https://news.ycombinator.com",
        "help": (
            "Algolia-hosted HN search index. No authentication required and no "
            "rate-limit ceiling for fair-use traffic. Nothing to configure here."
        ),
        "fields": [],
    },
    "microsoft_community": {
        "category": "source",
        "display": "Microsoft Tech Community (RSS)",
        "url": "https://techcommunity.microsoft.com",
        "help": (
            "Public RSS feeds. No authentication required. Nothing to configure "
            "here. Verify your feed URLs in each product's Sources page."
        ),
        "fields": [],
    },
    "stackex": {
        "category": "source",
        "display": "Stack Exchange",
        "url": "https://api.stackexchange.com/docs",
        "help": (
            "Public 2.3 REST API across Super User, Stack Overflow, Server Fault, "
            "etc. Anonymous mode allows 300 requests/day. Register an app at "
            "stackapps.com/apps/oauth/register (no approval wait, seconds to get "
            "a key) to raise it to 10,000/day."
        ),
        "fields": [
            {"env": "STACKEX_KEY", "label": "API key (optional)", "type": "secret", "default": "",
             "help": "Raises daily quota from 300 to 10,000 requests. Register at stackapps.com/apps/oauth/register — no approval wait."},
        ],
    },
    "apple_appstore": {
        "category": "source",
        "display": "Apple App Store",
        "url": "https://apps.apple.com",
        "help": (
            "Customer reviews via the public iTunes RSS/JSON feed. No auth "
            "required. Reviews are per country per app: ~500 most-recent "
            "reviews are available per country. Configure one stream per app; "
            "add multiple countries to a single stream if you want regional coverage."
        ),
        "fields": [],
    },
    "producthunt": {
        "category": "source",
        "display": "Product Hunt",
        "url": "https://api.producthunt.com/v2/docs",
        "help": (
            "GraphQL v2 API. Register an app at api.producthunt.com/v2/oauth/applications "
            "and click 'Create Token' on the app's page to get a bearer token that "
            "never expires. Rate limit: 900 complexity points / 15 minutes — plenty "
            "for typical usage."
        ),
        "fields": [
            {"env": "PRODUCTHUNT_TOKEN", "label": "Bearer token", "type": "secret", "default": "",
             "help": "Personal developer token from api.producthunt.com/v2/oauth/applications. Required."},
        ],
    },
    "rss": {
        "category": "source",
        "display": "Reddit RSS",
        "url": "https://www.reddit.com",
        "help": (
            "Reddit's per-subreddit RSS feed (also works with any other public "
            "RSS/Atom URL — news sites, blogs, Substack, Beehiiv). Zero auth. "
            "Best used as a Reddit fallback when the OAuth Data API isn't set "
            "up: paste one subreddit URL per stream, e.g. "
            "https://www.reddit.com/r/Windows11/new.rss. The connector "
            "auto-cleans Reddit's HTML wrapper and uses your REDDIT_USER_AGENT "
            "from .env (if set) to avoid rate limits."
        ),
        "fields": [],
    },
    "youtube_comments": {
        # (superseded — see MANIFEST in sources/youtube_comments.py)
        "category": "source",
    },
}

# LLM providers: not yet manifest-driven — hardcoded until LLMAdapterManifest
# lands. These entries are what's actually used for LLM connections.
_LLM_CONNECTION_META = {
    "anthropic": {
        "category": "llm",
        "display": "Anthropic Claude",
        "url": "https://console.anthropic.com",
        "api_endpoint": "https://api.anthropic.com/v1",
        "endpoint_hints": ["anthropic"],
        "help": (
            "Anthropic Claude via the OpenAI-compatible /v1 endpoint. Create a "
            "key at https://console.anthropic.com/account/keys. Recommended "
            "model pairing: Haiku 4.5 for relevance, Sonnet 4.6 (or Opus 4.7) "
            "for classify. Endpoint: https://api.anthropic.com/v1"
        ),
        "fields": [
            {"env": "ANTHROPIC_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Starts with 'sk-ant-…'. Treated as a credential."},
        ],
        "recommended_models": [
            {"id": "claude-haiku-4-5-20251001",
             "purpose": "relevance — cheap, fast"},
            {"id": "claude-sonnet-4-6",
             "purpose": "classify — balanced cost / quality (recommended default)"},
            {"id": "claude-opus-4-7",
             "purpose": "classify — highest quality, more expensive"},
        ],
    },
    "openai": {
        "category": "llm",
        "display": "OpenAI",
        "url": "https://platform.openai.com",
        "api_endpoint": "https://api.openai.com/v1",
        "endpoint_hints": ["openai.com"],
        "help": (
            "OpenAI ChatGPT models. Create a key at "
            "https://platform.openai.com/api-keys. Recommended pairing: "
            "gpt-4o-mini for relevance, gpt-4o (or gpt-4.1) for classify. "
            "Endpoint: https://api.openai.com/v1"
        ),
        "fields": [
            {"env": "OPENAI_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Starts with 'sk-…' or 'sk-proj-…'. Treated as a credential."},
        ],
        "recommended_models": [
            {"id": "gpt-4o-mini",
             "purpose": "relevance — cheap, fast"},
            {"id": "gpt-4o",
             "purpose": "classify — balanced default"},
            {"id": "gpt-4.1",
             "purpose": "classify — newer, stronger"},
            {"id": "o1-mini",
             "purpose": "classify — reasoning model (slow, expensive; overkill for most cases)"},
        ],
    },
    "google_gemini": {
        "category": "llm",
        "display": "Google Gemini",
        "url": "https://ai.google.dev",
        "api_endpoint": "https://generativelanguage.googleapis.com/v1beta/openai",
        "endpoint_hints": ["googleapis.com", "gemini"],
        "help": (
            "Google Gemini via the OpenAI-compatible /v1beta/openai endpoint. "
            "Get a free key at https://aistudio.google.com/app/apikey. "
            "Recommended pairing: Flash for relevance, Pro for classify. "
            "Endpoint: https://generativelanguage.googleapis.com/v1beta/openai"
        ),
        "fields": [
            {"env": "GOOGLE_API_KEY", "label": "API key", "type": "secret", "default": "",
             "help": "Google AI Studio key. Treated as a credential."},
        ],
        "recommended_models": [
            {"id": "gemini-2.0-flash",
             "purpose": "relevance — cheap, fast"},
            {"id": "gemini-2.0-pro",
             "purpose": "classify — balanced default"},
            {"id": "gemini-1.5-flash",
             "purpose": "relevance — fallback if 2.0 unavailable"},
        ],
    },
    "ollama": {
        "category": "llm",
        "display": "Ollama (local)",
        "url": "https://ollama.com",
        "api_endpoint": "http://localhost:11434/v1",
        "endpoint_hints": ["11434", "ollama"],
        "help": (
            "Local cross-platform LLM runtime. No credentials needed — just "
            "have `ollama serve` running and the model pulled "
            "(`ollama pull phi4-mini`). Default endpoint: "
            "http://localhost:11434/v1. Override the URL only if you've "
            "remapped Ollama's port or are pointing at a remote Ollama host."
        ),
        "fields": [
            {"env": "OLLAMA_BASE_URL", "label": "Base URL override (optional)", "type": "text",
             "default": "http://localhost:11434/v1",
             "help": "Leave empty to use the LLM-routing endpoint as-is. Set to override globally."},
        ],
        "recommended_models": [
            {"id": "phi4-mini",  "purpose": "relevance + classify — small (~2 GB)"},
            {"id": "phi3",       "purpose": "relevance + classify — small (~2 GB)"},
            {"id": "llama3.2",   "purpose": "classify — capable mid-size"},
            {"id": "qwen2.5",    "purpose": "classify — strong on instruction following"},
            {"id": "mistral",    "purpose": "classify — popular (~4 GB)"},
        ],
        "model_note": (
            "Ollama models must be pulled first: `ollama pull <id>`. The "
            "Connections page lists what's already pulled on this machine."
        ),
    },
    "foundry_local": {
        "category": "llm",
        "display": "Foundry Local (Windows local)",
        "url": "https://learn.microsoft.com/en-us/azure/ai-studio/foundry-local/",
        "api_endpoint": "http://localhost:5273/v1",
        "endpoint_hints": ["5273", "foundry"],
        "help": (
            "Microsoft Foundry Local. Windows-only. Default endpoint: "
            "http://localhost:5273/v1. No credentials. Override the URL only "
            "if you've remapped the port."
        ),
        "fields": [
            {"env": "FOUNDRY_BASE_URL", "label": "Base URL override (optional)", "type": "text",
             "default": "http://localhost:5273/v1",
             "help": "Leave empty to use the LLM-routing endpoint as-is."},
        ],
        "recommended_models": [
            {"id": "phi-4-mini", "purpose": "relevance + classify — Microsoft's small model"},
            {"id": "phi-4",      "purpose": "classify — larger Phi-4"},
        ],
    },
}


# --- Registry-driven CONNECTION_META and SOURCE_TYPE_META -------------------
# Source metadata now lives in each plugin's MANIFEST (POST_V1_PLAN §4.1,
# ADR-0001). Templates and route handlers still consume the same dict shapes,
# so we build those from the registry at module import.


def _manifest_to_connection_meta(manifest) -> dict:
    """Convert a SourceManifest → the CONNECTION_META entry shape templates expect."""
    return {
        "category": "source" if manifest.category == "source" else manifest.category,
        "display": manifest.display_name,
        "url": manifest.docs_url or "",
        "help": manifest.help,
        "fields": [
            {
                "env": f.name,
                "label": f.label,
                "type": f.type,
                "default": f.default if f.default is not None else "",
                "help": f.help,
            }
            for f in manifest.connection_fields
        ],
    }


def _manifest_to_source_type_meta(manifest) -> dict:
    """Convert a SourceManifest → the SOURCE_TYPE_META entry shape templates expect."""
    return {
        "display": manifest.display_name,
        "help": manifest.help,
        "stream_fields": [
            {
                "name": f.name,
                "label": f.label,
                "type": f.type,
                "required": f.required,
                "default": f.default if f.default is not None else "",
                "placeholder": f.placeholder or "",
                "help": f.help,
            }
            for f in manifest.stream_fields
        ],
    }


_ASSISTANT_LLM_CONNECTION_META = {
    "assistant_llm": {
        "category": "assistant_llm",
        "display": "Assistant LLM (global)",
        "url": "",
        "help": (
            "Global LLM used by the guided setup wizard, snippet candidate "
            "discovery, and prompt suggestions. Distinct from per-product "
            "routing so the wizard has an LLM before per-product config "
            "exists. Configured once at /connections/assistant_llm."
        ),
        "fields": [],   # dedicated form, not env-var-only
    },
}


# ScrapeCreators plugins all share ONE API key (SCRAPECREATORS_API_KEY) via
# the shared client in sources/scrapecreators/client.py. On the Connections
# page we collapse the three sub-plugin rows into this single synthetic
# entry so users don't see three redundant "1 field" rows for the same key.
# The individual scrapecreators_reddit/x/tiktok manifests still exist for
# product sources.yaml, the "add source" picker, and per-plugin stream config.
_SCRAPECREATORS_SUBPLUGINS = ("scrapecreators_reddit", "scrapecreators_x", "scrapecreators_tiktok")

_SCRAPECREATORS_CONNECTION_META = {
    "scrapecreators": {
        "category": "source",
        "display": "ScrapeCreators",
        "url": "https://scrapecreators.com/",
        "help": (
            "Shared API key for the Reddit / X / TikTok ScrapeCreators plugins. "
            "One key covers all three; the individual plugins read from it via "
            "the shared client. Pause here to disable all three at once."
        ),
        "fields": [
            {
                "env": "SCRAPECREATORS_API_KEY",
                "label": "ScrapeCreators API Key",
                "type": "secret",
                "default": "",
                "help": "One key covers Reddit + X + TikTok SC plugins. Get it at scrapecreators.com.",
            },
        ],
    },
}


def _build_meta_dicts() -> tuple[dict[str, dict], dict[str, dict]]:
    """Compute CONNECTION_META and SOURCE_TYPE_META from registered plugins.

    CONNECTION_META = LLM providers (still hardcoded) + source plugins (from registry).
    SOURCE_TYPE_META = source plugins only (LLM providers don't have stream fields).
    """
    from sources.registry import get_registry

    conn_meta: dict[str, dict] = dict(_LLM_CONNECTION_META)
    conn_meta.update(_ASSISTANT_LLM_CONNECTION_META)
    conn_meta.update(_SCRAPECREATORS_CONNECTION_META)
    src_type_meta: dict[str, dict] = {}
    for plugin in get_registry().all_plugins():
        m = plugin.manifest
        conn_meta[m.plugin_id] = _manifest_to_connection_meta(m)
        src_type_meta[m.plugin_id] = _manifest_to_source_type_meta(m)
    return conn_meta, src_type_meta


# Computed once at import. Registry is a singleton; if a plugin is added to
# the plugins/ dir at runtime, restart the webui to pick it up.
CONNECTION_META, SOURCE_TYPE_META = _build_meta_dicts()


def _llm_providers_for_template() -> list[dict]:
    """Compact provider list for the LLM routing form's JS, used to map
    a free-text endpoint to its provider and surface recommended models."""
    out = []
    for type_id, meta in CONNECTION_META.items():
        if meta.get("category") != "llm":
            continue
        out.append({
            "type": type_id,
            "display": meta.get("display") or type_id,
            "api_endpoint": meta.get("api_endpoint") or "",
            "endpoint_hints": meta.get("endpoint_hints") or [],
            "models": [
                {"id": m["id"], "purpose": m.get("purpose", "")}
                for m in (meta.get("recommended_models") or [])
            ],
        })
    return out


def _detect_provider(endpoint: str) -> str:
    """Return the provider type id whose endpoint_hints match `endpoint`,
    or 'custom' if none match. Mirrors the client-side _matchProvider."""
    if not endpoint:
        return "custom"
    low = endpoint.lower()
    for type_id, meta in CONNECTION_META.items():
        if meta.get("category") != "llm":
            continue
        for hint in (meta.get("endpoint_hints") or []):
            if hint and hint.lower() in low:
                return type_id
    return "custom"


def _read_env() -> dict[str, str]:
    """Return current values in .env (empty dict if the file doesn't exist)."""
    if not ENV_FILE_PATH.exists():
        return {}
    return {k: (v or "") for k, v in dotenv_values(str(ENV_FILE_PATH)).items()}


def _connection_status(type_id: str, env: dict[str, str]) -> str:
    """One-word status for the list view: configured / partial / not-needed / missing."""
    meta = CONNECTION_META.get(type_id, {})
    fields = meta.get("fields") or []
    if not fields:
        return "not-needed"
    set_fields = sum(1 for f in fields if (env.get(f["env"]) or "").strip())
    if set_fields == len(fields):
        return "configured"
    if set_fields == 0:
        return "missing"
    return "partial"


# Display metadata for the ADR-0021 source_category taxonomy. `id` is
# what the template loops over; `display` is the section header. `plugin_ids`
# is derived at request time from each plugin's manifest — no hand-maintained
# set. The synthetic "scrapecreators" row is always classified as
# third_party_scraper via the manual mapping in _source_group_id.
SOURCE_GROUPS: list[dict] = [
    {
        "id": "rss_feed",
        "display": "RSS feeds",
        "description": "Keyless — no API key needed. Fetched via RSS/Atom.",
    },
    {
        "id": "third_party_scraper",
        "display": "Third-party scrapers",
        "description": "Social platforms (Reddit, X, TikTok) fetched via a shared scraper API vendor.",
    },
    {
        "id": "custom_source",
        "display": "Custom sources",
        "description": "Each plugin requires its own API key or OAuth credentials. Configure per plugin.",
    },
]


# Synthetic rollup rows (scrapecreators) don't have a real manifest to read
# source_category from, so we map them explicitly.
_SYNTHETIC_TYPE_TO_CATEGORY = {
    "scrapecreators": "third_party_scraper",
}


def _source_group_id(type_id: str) -> str:
    """Map a plugin id to its display group by reading its manifest's
    `source_category`. Falls back to `custom_source` for unknown ids so the
    row shows up on the page rather than silently disappearing.
    """
    if type_id in _SYNTHETIC_TYPE_TO_CATEGORY:
        return _SYNTHETIC_TYPE_TO_CATEGORY[type_id]
    try:
        from sources.registry import get_registry
        p = get_registry().get(type_id)
        if p is not None:
            return getattr(p.manifest, "source_category", "custom_source")
    except Exception:
        pass
    return "custom_source"


def _row_for_connection(type_id: str, meta: dict, env: dict, paused_types: set) -> dict:
    """Uniform row shape for both the sources and LLM connection tables."""
    return {
        "type": type_id,
        "display": meta.get("display") or type_id,
        "url": meta.get("url") or "",
        "n_fields": len(meta.get("fields") or []),
        "status": _connection_status(type_id, env),
        "paused": type_id in paused_types,
        # Only source-type rows get a pause toggle; LLM providers don't.
        "can_pause": meta.get("category") == "source",
    }


@app.get("/connections", response_class=HTMLResponse)
def connections_index(request: Request):
    """Sources tab (default landing at /connections). LLM providers +
    assistant LLM live on /connections/llms."""
    env = _read_env()
    from sources import available_source_types
    from pipeline import connections as _conn
    from pipeline import media_sources as _media

    available_sources = set(available_source_types())
    globally_paused = _conn.paused_types()

    source_rows: list[dict] = []
    for type_id, meta in CONNECTION_META.items():
        if meta.get("category") == "source" and type_id in available_sources:
            source_rows.append(_row_for_connection(type_id, meta, env, globally_paused))

    # Collapse the 3 ScrapeCreators sub-plugin rows into ONE synthetic row.
    # They share SCRAPECREATORS_API_KEY (via sources/scrapecreators/client.py),
    # so exposing three identical "1 field" entries just confuses users.
    sc_present = any(p in available_sources for p in _SCRAPECREATORS_SUBPLUGINS)
    source_rows = [r for r in source_rows if r["type"] not in _SCRAPECREATORS_SUBPLUGINS]
    if sc_present:
        sc_meta = CONNECTION_META["scrapecreators"]
        sc_row = _row_for_connection("scrapecreators", sc_meta, env, globally_paused)
        # Synthetic row is "paused" only when every sub-plugin is paused —
        # partial pause (e.g. only TikTok muted) shows as active with a note.
        sc_row["paused"] = all(p in globally_paused for p in _SCRAPECREATORS_SUBPLUGINS)
        source_rows.append(sc_row)

    source_rows.sort(key=lambda r: r["display"])

    grouped_sources: dict[str, list[dict]] = {g["id"]: [] for g in SOURCE_GROUPS}
    for r in source_rows:
        gid = _source_group_id(r["type"])
        # Unknown category → drop into custom_source rather than KeyError.
        if gid not in grouped_sources:
            gid = "custom_source"
        grouped_sources[gid].append(r)

    # Also expose plugin content_types so the template can render chips.
    from sources.registry import get_registry as _reg
    _r = _reg()
    for rows in grouped_sources.values():
        for row in rows:
            plugin = _r.get(row["type"])
            if plugin is not None:
                row["content_types"] = list(getattr(plugin.manifest, "content_types", []))
            else:
                row["content_types"] = []

    media_source_rows = _media.load()

    return templates.TemplateResponse(
        "connections_index.html",
        {
            "request": request,
            "source_rows": source_rows,
            "source_groups": SOURCE_GROUPS,
            "grouped_sources": grouped_sources,
            "media_sources": media_source_rows,
            "env_file": str(ENV_FILE_PATH),
            "admin_active": "sources",
        },
    )


@app.get("/connections/llms", response_class=HTMLResponse)
def connections_llms(request: Request):
    """LLM Connections tab — sibling to /connections (which is the Sources
    tab). Split out from the original /connections page in a UX pass because
    the combined view had become long."""
    env = _read_env()
    from pipeline import admin_defaults as _defaults
    from pipeline import assistant_llm as _al
    from pipeline import features as _features

    llm_rows: list[dict] = []
    for type_id, meta in CONNECTION_META.items():
        if meta.get("category") == "llm":
            # LLM rows don't participate in the source pause list.
            llm_rows.append(_row_for_connection(type_id, meta, env, set()))
    llm_rows.sort(key=lambda r: r["display"])

    cfg = _al.current_config()
    assistant_summary = {
        "configured": cfg is not None,
        "endpoint": cfg.endpoint if cfg else "",
        "model": cfg.model if cfg else "",
        "budget_usd": cfg.budget_usd_per_product_per_month if cfg else 0.0,
        "api_key_env": cfg.api_key_env if cfg else "",
        "enabled": _features.enabled("assistant_llm_enabled"),
    }

    # Only *configured* rows are selectable so we don't let the admin
    # nominate a provider whose key isn't set.
    llm_default_options = [
        {"type": r["type"], "display": r["display"]}
        for r in llm_rows if r["status"] == "configured"
    ]
    current_default = _defaults.default_llm_provider()

    return templates.TemplateResponse(
        "connections_llms.html",
        {
            "request": request,
            "llm_rows": llm_rows,
            "env_file": str(ENV_FILE_PATH),
            "assistant_summary": assistant_summary,
            "admin_active": "llms",
            "llm_default_options": llm_default_options,
            "current_default": current_default,
            "default_saved": request.query_params.get("default_saved") == "1",
        },
    )


@app.post("/connections/default_llm")
def connections_set_default_llm(provider: str = Form("")):
    """Persist the admin's choice of default LLM provider — the one the
    wizard's Review screen pre-selects. Empty string clears the default."""
    from pipeline import admin_defaults as _defaults
    _defaults.set_default_llm_provider(provider.strip() or None)
    # Redirects to the LLM Connections tab (where the default LLM selector
    # lives after the sources/llms split).
    return RedirectResponse(url="/connections/llms?default_saved=1", status_code=303)


# POST_V1_PLAN §4.8 — dedicated assistant LLM form (must register BEFORE
# the generic /connections/{type_id} route so FastAPI matches this first).


@app.get("/connections/assistant_llm", response_class=HTMLResponse)
def assistant_llm_form(request: Request, saved: int = 0, error: Optional[str] = None):
    """Dedicated form for the global assistant LLM (POST_V1_PLAN §4.8)."""
    from pipeline import assistant_llm as _al

    cfg = _al.current_config()
    return templates.TemplateResponse(
        "assistant_llm_form.html",
        {
            "request": request,
            "cfg": cfg,
            "configured": _al.is_configured(),
            "llm_providers": _llm_providers_for_template(),
            "saved": bool(saved),
            "error": error,
        },
    )


@app.post("/connections/assistant_llm")
async def assistant_llm_save(request: Request):
    from pipeline import assistant_llm as _al

    form = dict(await request.form())
    try:
        cfg = _al.AssistantLLMConfig(
            endpoint=form.get("endpoint", "").strip(),
            model=form.get("model", "").strip(),
            temperature=float(form.get("temperature", "0.2") or 0.2),
            seed=int(form["seed"]) if form.get("seed", "").strip() else None,
            timeout_seconds=int(form.get("timeout_seconds", "60") or 60),
            max_retries=int(form.get("max_retries", "3") or 3),
            budget_usd_per_product_per_month=float(
                form.get("budget_usd_per_product_per_month", "10.0") or 10.0
            ),
        )
        if not cfg.endpoint or not cfg.model:
            raise ValueError("endpoint and model are required")
    except (ValueError, KeyError) as e:
        return RedirectResponse(
            url=f"/connections/assistant_llm?error={str(e)[:200]}",
            status_code=303,
        )

    _al.save_config(cfg)
    return RedirectResponse(url="/connections/assistant_llm?saved=1", status_code=303)


@app.get("/connections/{type_id}", response_class=HTMLResponse)
def connection_form(request: Request, type_id: str, error: Optional[str] = None,
                     saved: Optional[str] = None, assistant: Optional[str] = None):
    if type_id not in CONNECTION_META:
        raise HTTPException(status_code=404, detail=f"unknown source type: {type_id}")
    meta = CONNECTION_META[type_id]
    env = _read_env()
    values: dict[str, str] = {}
    for f in meta.get("fields") or []:
        values[f["env"]] = env.get(f["env"], "") or f.get("default", "")

    # Is this connection currently the one powering the assistant LLM?
    # Compare endpoints (case-insensitive, ignore trailing slash) so an
    # admin who set up the assistant via this same provider sees the
    # checkbox already checked.
    is_current_assistant = False
    if meta.get("category") == "llm":
        try:
            from pipeline import assistant_llm as _al
            cfg = _al.current_config()
            if cfg and cfg.endpoint:
                a = cfg.endpoint.rstrip("/").lower()
                b = (meta.get("api_endpoint") or "").rstrip("/").lower()
                is_current_assistant = bool(a and b and a == b)
        except Exception:
            pass

    return templates.TemplateResponse(
        "connection_form.html",
        {
            "request": request,
            "type_id": type_id,
            "meta": meta,
            "values": values,
            "error": error,
            "saved": saved,
            "assistant_saved": assistant == "1",
            "is_current_assistant": is_current_assistant,
            "env_file": str(ENV_FILE_PATH),
        },
    )


# --- Ollama lifecycle API ---------------------------------------------------


@app.post("/api/ollama/ensure-running")
def api_ollama_ensure_running(payload: dict = Body(default={})):
    """Detect Ollama, spawn `ollama serve` if needed, return readiness +
    pulled-models list. Called from the connections/ollama page and from
    the LLM routing form on save."""
    from pipeline import ollama_lifecycle
    base_url = (payload or {}).get("base_url") or "http://localhost:11434"
    required_model = (payload or {}).get("required_model") or None
    return ollama_lifecycle.ensure_running(base_url=base_url, required_model=required_model)


@app.post("/api/ollama/pull")
def api_ollama_pull(payload: dict = Body(default={})):
    """Stream Ollama's POST /api/pull progress back as Server-Sent Events.

    The UI calls this when the user clicks "Pull model now" in the LLM
    routing save dialog. Each Ollama JSON line is emitted as one SSE
    `data:` frame so the browser can show a progress bar.
    """
    import json as _json
    from pipeline import ollama_lifecycle
    name = ((payload or {}).get("name") or "").strip()
    base_url = (payload or {}).get("base_url") or "http://localhost:11434"

    def _events():
        for evt in ollama_lifecycle.pull_model(name, base_url=base_url):
            yield f"data: {_json.dumps(evt)}\n\n"

    return StreamingResponse(_events(), media_type="text/event-stream")


@app.post("/api/ollama/install")
def api_ollama_install(payload: dict = Body(default={})):
    """Install Ollama via the official upstream installer for this platform.
    Idempotent — returns ok=true with a message if already installed.

    On Windows: downloads + runs OllamaSetup.exe /SILENT (per-user install,
    no UAC). On macOS / Linux: downloads + pipes install.sh into sh.

    Synchronous: may take 30-120 seconds. The UI button polls this and
    surfaces the status payload inline.
    """
    from pipeline import ollama_lifecycle
    return ollama_lifecycle.install_ollama()


@app.post("/connections/{type_id}")
async def connection_save(type_id: str, request: Request):
    if type_id not in CONNECTION_META:
        raise HTTPException(status_code=404, detail=f"unknown source type: {type_id}")
    meta = CONNECTION_META[type_id]
    form = await request.form()

    # Make sure the .env file exists; dotenv.set_key creates it if absent
    # but its parent must exist. Project root always does.
    ENV_FILE_PATH.touch(exist_ok=True)

    # Capture the API key BEFORE writing it — we may also copy it to
    # ASSISTANT_LLM_API_KEY below if the user asked to reuse this
    # connection for the assistant LLM.
    saved_secrets: dict[str, str] = {}

    try:
        from pipeline import env_writer
        for f in meta.get("fields") or []:
            env_name = f["env"]
            new_val = (form.get(env_name) or "").strip()
            # Empty value -> unset the key entirely (cleaner than KEY=).
            # env_writer keeps os.environ + the .env file in sync so the
            # running process reads the new value on the NEXT request
            # rather than after a restart.
            env_writer.set_or_unset(ENV_FILE_PATH, env_name, new_val)
            if new_val:
                saved_secrets[env_name] = new_val
    except Exception as e:
        return RedirectResponse(
            url=f"/connections/{type_id}?error={str(e)[:200]}",
            status_code=303,
        )

    # "Use for Assistant LLM": one-click way to point the assistant LLM at
    # the same provider using the same key. Copies endpoint + default model
    # into config/assistant_llm.yaml AND writes ASSISTANT_LLM_API_KEY to
    # .env so the assistant reads from its own env slot (kept distinct from
    # the connections env var — same key, different var, so the two can be
    # rotated independently later).
    use_for_assistant = (form.get("use_for_assistant") == "on"
                          and meta.get("category") == "llm")
    if use_for_assistant:
        try:
            from pipeline import assistant_llm as _al
            from pipeline import features as _features
            endpoint = meta.get("api_endpoint", "")
            # Prefer the classify-tier recommended model if one is listed;
            # otherwise fall back to the first recommendation.
            models = meta.get("recommended_models") or []
            default_model = ""
            for m in models:
                if "recommended default" in (m.get("purpose") or "").lower():
                    default_model = m.get("id", ""); break
            if not default_model and models:
                default_model = models[0].get("id", "")
            if endpoint and default_model:
                cfg = _al.AssistantLLMConfig(
                    endpoint=endpoint, model=default_model,
                    api_key_env="ASSISTANT_LLM_API_KEY",
                )
                _al.save_config(cfg)
                # Copy the just-saved API key value into ASSISTANT_LLM_API_KEY
                # so the assistant sees it immediately.
                for env_name, value in saved_secrets.items():
                    if value:
                        env_writer.set_var(ENV_FILE_PATH,
                                           "ASSISTANT_LLM_API_KEY", value)
                        break
                # Turn on the flag so the wizard actually uses it.
                try:
                    _flip_flag_on("assistant_llm_enabled")
                except Exception:
                    pass
        except Exception as e:
            return RedirectResponse(
                url=f"/connections/{type_id}?error=assistant+copy+failed:+{str(e)[:120]}",
                status_code=303,
            )

    return RedirectResponse(
        url=(f"/connections/{type_id}?saved=1"
              + ("&assistant=1" if use_for_assistant else "")),
        status_code=303,
    )


def _flip_flag_on(name: str) -> None:
    """Turn a feature flag ON in config/features.yaml. Used by
    connection_save when the admin asks to reuse the connection for the
    assistant LLM — we don't want to turn it on without clearing the
    'not configured' banner in the wizard."""
    from pipeline import features as _features
    try:
        data = yaml.safe_load(_FEATURES_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    flags = data.get("features") or {}
    if flags.get(name) is True:
        return
    flags[name] = True
    data["features"] = flags
    tmp = _FEATURES_YAML.with_suffix(_FEATURES_YAML.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
                   encoding="utf-8")
    tmp.replace(_FEATURES_YAML)
    _features.clear_cache()


@app.post("/connections/{type_id}/pause")
def connection_toggle_pause(type_id: str, paused: str = Form(...)):
    """Set the global pause state for a source type.

    Called from the /connections index page's per-row pause form. `paused`
    is 'true' or 'false' (string form value). Only source-type connections
    can be paused — LLM providers are always active. Precedence: the global
    pause here overrides any product-level `paused: false`.

    Special case: `type_id == "scrapecreators"` is a synthetic UI row
    covering the 3 real ScrapeCreators sub-plugins. Pause/unpause fans out
    to all three so the visible "paused" state on the collapsed row matches
    reality.
    """
    if type_id not in CONNECTION_META:
        raise HTTPException(status_code=404, detail=f"unknown connection type: {type_id}")
    if CONNECTION_META[type_id].get("category") != "source":
        raise HTTPException(status_code=400, detail="only source connections can be paused")
    from pipeline import connections as _conn
    is_paused = paused.lower() in ("true", "1", "on", "yes")
    if type_id == "scrapecreators":
        for sub in _SCRAPECREATORS_SUBPLUGINS:
            _conn.set_paused(sub, is_paused)
    else:
        _conn.set_paused(type_id, is_paused)
    return RedirectResponse(url="/connections", status_code=303)


# --- Sources form (Phase 5) -------------------------------------------------
#
# Per-type form layout so a non-YAML user can add / remove source instances
# and their streams. Type-specific stream fields come from each plugin's
# MANIFEST (POST_V1_PLAN §4.1); SOURCE_TYPE_META is now built above from the
# registry (see _build_meta_dicts).

_LEGACY_SOURCE_TYPE_META_UNUSED: dict[str, dict] = {
    "reddit": {
        "display": "Reddit",
        "help": (
            "Subreddit-based ingest via PRAW. Needs Reddit non-commercial API "
            "approval and REDDIT_CLIENT_ID/SECRET in .env. Each stream is one "
            "subreddit."
        ),
        "stream_fields": [
            {"name": "subreddit", "label": "Subreddit", "type": "text", "required": True,
             "placeholder": "Windows11", "help": "Subreddit name, no r/ prefix."},
            {"name": "display", "label": "Display label", "type": "text", "required": False,
             "placeholder": "r/Windows11",
             "help": "Human-readable name shown in reports. Defaults to r/<subreddit>."},
            {"name": "engagement_threshold", "label": "Engagement threshold", "type": "number", "required": False, "default": 5,
             "help": "Minimum upvotes+comments needed for an item to survive the heuristic filter. Lower = more items + more noise."},
        ],
    },
    "hn": {
        "display": "Hacker News",
        "help": (
            "Algolia-backed HN search. No auth. Each stream is a list of "
            "search queries the connector iterates."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "hn-windows", "help": "Internal label for cursor / dedup; doesn't have to be unique across products."},
            {"name": "search_queries", "label": "Search queries (one per line)", "type": "textarea_list", "required": True,
             "placeholder": "windows 11\nKB5036980\nmicrosoft copilot",
             "help": "One Lucene-style query per line. Each is paginated independently."},
            {"name": "include_tags", "label": "Include tags (comma list)", "type": "csv", "required": False, "default": "story",
             "help": "story | comment | story,comment. story-only avoids comment-without-parent-context noise."},
            {"name": "max_pages_per_query", "label": "Max pages per query", "type": "number", "required": False, "default": 5,
             "help": "Algolia caps at 1000 results per query; a page is `hits_per_page` items."},
            {"name": "hits_per_page", "label": "Hits per page", "type": "number", "required": False, "default": 100,
             "help": "1-200. 100 is the recommended sweet spot."},
        ],
    },
    "github_issues": {
        "display": "GitHub Issues",
        "help": (
            "GitHub REST /repos/{owner}/{repo}/issues. Needs a fine-grained "
            "PAT in .env as GITHUB_TOKEN (Public Repositories, read-only). "
            "Each stream is a set of repos."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "microsoft-dev-tools", "help": "Internal label for cursor / dedup."},
            {"name": "repos", "label": "Repos (one per line, owner/repo)", "type": "textarea_list", "required": True,
             "placeholder": "microsoft/PowerToys\nmicrosoft/terminal\nmicrosoft/WSL",
             "help": "Each line is one repo. Cursor advances on MAX(updated_at) across them; dedup catches the small overlap."},
            {"name": "include_labels", "label": "Include only labels (comma list)", "type": "csv", "required": False, "default": "",
             "help": "Empty = all issues. If set, only issues with at least one of these labels are kept."},
            {"name": "exclude_labels", "label": "Exclude labels (comma list)", "type": "csv", "required": False, "default": "duplicate,wontfix",
             "help": "Drop issues with any of these labels. Defaults exclude obvious noise."},
            {"name": "fetch_comments", "label": "Fetch comments", "type": "bool", "required": False, "default": True,
             "help": "Fetch comments on each issue. Adds API calls but gives the classifier more context."},
            {"name": "max_comments_per_issue", "label": "Max comments per issue", "type": "number", "required": False, "default": 50,
             "help": "Safety cap on hot threads. Older comments past the cap are dropped."},
        ],
    },
    "apple_appstore": {
        "display": "Apple App Store",
        "help": (
            "Customer reviews via the public iTunes RSS/JSON feed. No auth. "
            "One stream per app you want to monitor; add multiple countries "
            "to the same stream to get regional coverage. Filter by rating "
            "if you only care about complaints (1-2 stars) vs. all reviews."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "netflix-us", "help": "Internal label for cursor / dedup."},
            {"name": "app_id", "label": "Apple app id", "type": "text", "required": True,
             "placeholder": "363590051",
             "help": "The numeric id from the App Store URL (apps.apple.com/us/app/…/id{THIS}). Copy just the digits."},
            {"name": "countries", "label": "Countries (comma list)", "type": "csv", "required": False, "default": "us",
             "help": "ISO country codes: us, gb, ca, de, fr, jp, kr, in, ... Each is a separate ~500-review pool."},
            {"name": "max_pages", "label": "Max pages per country", "type": "number", "required": False, "default": 10,
             "help": "Apple caps at 10 pages (~500 reviews). Lower this if you only care about the latest N reviews."},
            {"name": "min_rating", "label": "Minimum rating", "type": "number", "required": False, "default": 0,
             "help": "0 = keep all. Set to 3 to drop 3-5 star reviews (keep only complaints)."},
            {"name": "max_rating", "label": "Maximum rating", "type": "number", "required": False, "default": 5,
             "help": "5 = keep all. Set to 2 for a 1-2 star rants-only stream."},
        ],
    },
    "youtube_comments": {
        "display": "YouTube Comments",
        "help": (
            "YouTube Data API v3, search-first flow. Each stream runs one or "
            "more keyword searches, then fetches comments (and full replies) "
            "on the returned videos. Quota-heavy — one search = 100 units, "
            "one comment page = 1 unit. Requires YOUTUBE_API_KEY in .env."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "windows-audio-search", "help": "Internal label for cursor / dedup."},
            {"name": "search_queries", "label": "Search queries (one per line)", "type": "textarea_list", "required": True,
             "placeholder": "windows 11 audio problems\nbluetooth headphones windows\nrealtek driver",
             "help": "One search per line. Each burns 100 units of your daily YouTube quota."},
            {"name": "max_videos_per_query", "label": "Max videos per query", "type": "number", "required": False, "default": 25,
             "help": "Cap on videos discovered per query. YouTube search returns up to 50 per call; lower cap saves comment-fetch quota."},
            {"name": "max_comments_per_video", "label": "Max comments per video", "type": "number", "required": False, "default": 200,
             "help": "Safety cap on hot threads (flagship reviews can have 50K+ comments). Higher = more signal but more quota."},
            {"name": "max_replies_per_thread", "label": "Max replies per thread", "type": "number", "required": False, "default": 100,
             "help": "commentThreads inlines 5 replies for free; this caps how many more we fetch via comments.list (1 unit per page)."},
            {"name": "search_order", "label": "Search order", "type": "text", "required": False, "default": "relevance",
             "help": "relevance | date. 'relevance' surfaces higher-quality videos; 'date' gets the newest."},
            {"name": "comment_order", "label": "Comment order", "type": "text", "required": False, "default": "relevance",
             "help": "relevance | time. 'relevance' surfaces highest-quality comments (YouTube's own ranking)."},
            {"name": "min_video_views", "label": "Min video views", "type": "number", "required": False, "default": 1000,
             "help": "Skip videos below this view count. Filters out obscure/low-engagement content."},
            {"name": "published_within_days", "label": "Only videos from last N days", "type": "number", "required": False, "default": 90,
             "help": "0 = no filter. Recommended: 90-180 for recency; longer wastes quota on stale videos."},
        ],
    },
    "rss": {
        "display": "Reddit RSS",
        "help": (
            "Reddit per-subreddit RSS feeds — the fallback when the OAuth "
            "Data API isn't configured. Paste one subreddit's RSS URL per "
            "stream, e.g. https://www.reddit.com/r/Windows11/new.rss. "
            "For multiple subreddits, set 'Sleep before fetch' to 10+ "
            "seconds each to avoid 429 rate limits, and set REDDIT_USER_AGENT "
            "in .env for a friendlier UA. "
            "The connector also accepts any public RSS/Atom URL (news sites, "
            "blogs, Substack, Beehiiv), so you can use it as a context layer too."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "windows-central", "help": "Internal label for cursor / dedup. Also the default display name."},
            {"name": "feed_url", "label": "Feed URL", "type": "text", "required": True,
             "placeholder": "https://www.windowscentral.com/rss.xml",
             "help": "Public RSS or Atom feed URL. For Reddit: https://www.reddit.com/r/SUBREDDIT/new.rss"},
            {"name": "display", "label": "Display label", "type": "text", "required": False, "default": "",
             "placeholder": "Windows Central",
             "help": "Human-readable name shown in reports. Defaults to the stream name."},
            {"name": "sleep_before_fetch_seconds", "label": "Sleep before fetch (seconds)", "type": "number", "required": False, "default": 0,
             "help": "Pause before this stream fetches. Useful when multiple streams target the same rate-limited host (Reddit: try 3-5)."},
        ],
    },
    "producthunt": {
        "display": "Product Hunt",
        "help": (
            "GraphQL v2. Requires PRODUCTHUNT_TOKEN in .env — get one at "
            "api.producthunt.com/v2/oauth/applications (Create Token). "
            "Streams are topic-filtered. Comments are the substantive "
            "feedback; the post body is mostly launch marketing copy."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "technology-launches", "help": "Internal label for cursor / dedup."},
            {"name": "topic_slug", "label": "Topic slug", "type": "text", "required": False, "default": "",
             "placeholder": "artificial-intelligence",
             "help": "Topic slug from producthunt.com/topics/{slug}. Empty = across all topics (usually too broad)."},
            {"name": "max_posts", "label": "Max posts per run", "type": "number", "required": False, "default": 50,
             "help": "Cap to keep API complexity budget reasonable. Each post also fetches its comments if enabled."},
            {"name": "fetch_comments", "label": "Fetch comments", "type": "bool", "required": False, "default": True,
             "help": "Emit each post's comments as child items. Comments are the substantive feedback."},
            {"name": "max_comments_per_post", "label": "Max comments per post", "type": "number", "required": False, "default": 50,
             "help": "Safety cap on hot threads. Older comments past the cap are skipped."},
        ],
    },
    "stackex": {
        "display": "Stack Exchange",
        "help": (
            "Stack Exchange 2.3 REST across Super User, Stack Overflow, and "
            "sibling sites. Optional STACKEX_KEY in .env raises the daily quota "
            "from 300 to 10K. Each stream is one (site, tags) pair; add a "
            "second stream for a second site. Unanswered questions with high "
            "views are the highest-signal slice — enable 'Unanswered only' for that."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "superuser-windows", "help": "Internal label for cursor / dedup."},
            {"name": "site", "label": "Site", "type": "text", "required": True,
             "placeholder": "superuser",
             "help": "Site slug: superuser | stackoverflow | serverfault | apple | unix | askubuntu | gaming | electronics."},
            {"name": "tags", "label": "Tags (comma or newline list)", "type": "csv", "required": False, "default": "",
             "help": "Tags are AND-joined at the API layer. Empty = all tags on that site (usually too broad — set at least one)."},
            {"name": "unanswered_only", "label": "Unanswered only", "type": "bool", "required": False, "default": False,
             "help": "Only fetch questions without an accepted answer. Highest signal for 'real unresolved pain.'"},
            {"name": "hydrate_answers", "label": "Also fetch answers", "type": "bool", "required": False, "default": False,
             "help": "Fetch answers for each kept question as child items. ~2x quota cost. Off by default."},
            {"name": "max_pages", "label": "Max pages per stream", "type": "number", "required": False, "default": 5,
             "help": "SE returns 100 items/page. Cap keeps a single stream from exhausting the daily quota."},
            {"name": "engagement_threshold", "label": "Engagement threshold", "type": "number", "required": False, "default": 0,
             "help": "Minimum (score + answer_count) to keep a question. 0 = no gate; the pipeline's filter stage handles the rest."},
        ],
    },
    "microsoft_community": {
        "display": "Microsoft Tech Community (RSS)",
        "help": (
            "Lithium-platform RSS for Microsoft Tech Community + Q&A. No auth. "
            "Each stream is one feed URL. Verify URLs against the live site — "
            "they break after redesigns."
        ),
        "stream_fields": [
            {"name": "name", "label": "Stream name", "type": "text", "required": True,
             "placeholder": "tech-community-windows", "help": "Internal label for cursor / dedup."},
            {"name": "display", "label": "Display label", "type": "text", "required": False,
             "placeholder": "Tech Community — Windows",
             "help": "Human-readable name shown in reports."},
            {"name": "feed_url", "label": "Feed URL", "type": "text", "required": True,
             "placeholder": "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/Category?category.id=Windows",
             "help": "The full RSS URL. The Windows category URL is the example shown."},
        ],
    },
}


# --- Flat-streams view for the redesigned Sources page ----------------------
#
# The underlying sources.yaml groups streams by source instance:
#   sources: [{id, type, paused, streams: [{...}, {...}]}]
# but the new UI presents a flat list — one row per stream — so users don't
# have to think about instance grouping. These helpers convert between shapes.


# For each source type, which stream-field is the "identifier" (the thing
# users think of as "the subreddit" or "the feed URL"). Used to build the
# preview shown in the flat table's Identifier column.
_TYPE_IDENTIFIER_FIELD: dict[str, str] = {
    "reddit":              "subreddit",
    "reddit_rss":          "subreddit",       # bare name; UI renders as r/name
    "hn":                  "search_queries",  # list; take first for label
    "github_issues":       "repos",           # list
    "microsoft_community": "feed_url",
    "stackex":             "tags",            # list
    "apple_appstore":      "app_id",
    "producthunt":         "topic_slug",
    "rss":                 "feed_url",
    "youtube_comments":    "search_queries",  # list
}


def _stream_identifier(stream_type: str, stream: dict) -> str:
    """Short human-readable label for the Identifier column."""
    key = _TYPE_IDENTIFIER_FIELD.get(stream_type)
    if not key:
        return stream.get("name") or "—"
    v = stream.get(key)
    # reddit_rss legacy compat: pre-migration streams may carry `feed_url`
    # without `subreddit`. Extract a display name from the URL if we can.
    if stream_type == "reddit_rss" and (v is None or v == ""):
        legacy = stream.get("feed_url") or ""
        if legacy:
            from sources.reddit_rss import normalize_subreddit
            v = normalize_subreddit(legacy) or legacy
    if isinstance(v, list):
        if not v:
            return "—"
        first = v[0]
        if len(v) == 1:
            return str(first)
        return f"{first} +{len(v) - 1} more"
    if v is None or v == "":
        return stream.get("name") or "—"
    if stream_type in ("reddit", "reddit_rss"):
        return f"r/{v}"
    if stream_type == "apple_appstore":
        countries = stream.get("countries") or ["us"]
        if isinstance(countries, list):
            countries = ",".join(countries[:3])
        return f"id={v} ({countries})"
    return str(v)


def _flat_streams(sources: list[dict], globally_paused: set[str]) -> list[dict]:
    """Flatten the sources list into per-stream rows, preserving enough info
    that a save can re-group them back into source instances."""
    rows: list[dict] = []
    for src in sources:
        stype = src.get("type") or ""
        instance_id = src.get("id") or ""
        instance_paused = bool(src.get("paused"))
        conn_paused = stype in globally_paused
        for si, stream in enumerate(src.get("streams") or []):
            stream_paused = bool(stream.get("paused"))
            # Effective status label — matches fetch.py precedence.
            if conn_paused:
                status = "paused (connection)"
            elif instance_paused:
                status = "paused (source)"
            elif stream_paused:
                status = "paused (stream)"
            else:
                status = "active"
            rows.append({
                "instance_id": instance_id,
                "stream_index": si,
                "type": stype,
                "identifier": _stream_identifier(stype, stream),
                "display": stream.get("display") or stream.get("name") or "",
                "status": status,
                "paused": stream_paused,           # per-stream pause (what the row toggles)
                "instance_paused": instance_paused, # for the "why is this paused" tooltip
                "connection_paused": conn_paused,
                "stream_data": stream,             # full dict for the Edit modal
            })
    return rows


def _build_source_cards(product, globally_paused: set[str]) -> list[dict]:
    """ADR-0021 taxonomy view: plugin cards + media catalog cards, each
    tagged with source_category + content_types so the template can group
    them under User Feedback / Media Coverage sections.

    The `rss` plugin itself is intentionally EXCLUDED — its user-facing
    equivalent is the individual media catalog entries (each feed = one
    card). Custom RSS URLs not in the catalog can only be added via the
    raw YAML editor.

    Card shapes are distinguished by `kind`:
      - "plugin"         — a registered Source plugin with stream_fields
      - "catalog_entry"  — a single media_sources.yaml entry
    """
    from sources.registry import get_registry
    from pipeline import media_sources as _media
    import os
    from dotenv import dotenv_values
    env_snapshot = dict(os.environ)
    env_p = Path(__file__).resolve().parent.parent / ".env"
    if env_p.exists():
        try:
            for k, v in (dotenv_values(env_p) or {}).items():
                if v:
                    env_snapshot.setdefault(k, v)
        except Exception:
            pass

    def _ready(manifest) -> bool:
        req = [f for f in manifest.connection_fields
               if getattr(f, "required", False) or getattr(f, "type", "") == "secret"]
        if not req:
            return True
        return all(env_snapshot.get(f.name, "").strip() for f in req)

    existing_by_type: dict[str, dict] = {
        s.get("type"): s for s in (product.sources or []) if s.get("type")
    }
    # Any rss stream on the product whose feed_url matches a catalog entry is
    # rendered as a catalog card, not under the rss plugin. Non-catalog rss
    # streams (custom URLs) surface only in the raw YAML editor.
    catalog_urls: set[str] = {e["feed_url"] for e in _media.load()}

    reg = get_registry()
    cards: list[dict] = []
    for plugin in reg.all_plugins():
        m = plugin.manifest
        if getattr(m, "category", "source") != "source":
            continue
        if m.plugin_id == "rss":
            continue   # excluded — see docstring
        if not _ready(m):
            continue
        instance = existing_by_type.get(m.plugin_id)
        conn_paused = m.plugin_id in globally_paused
        streams: list[dict] = []
        if instance:
            for si, stream in enumerate(instance.get("streams") or []):
                streams.append({
                    "index": si,
                    "data": stream,
                    "identifier": _stream_identifier(m.plugin_id, stream),
                    "display": stream.get("display") or stream.get("name") or "",
                    "paused": bool(stream.get("paused")),
                    "instance_paused": bool(instance.get("paused")),
                    "connection_paused": conn_paused,
                })
        stream_fields = [
            {
                "name": f.name, "label": f.label, "type": f.type,
                "required": f.required,
                "default": f.default if f.default is not None else "",
                "placeholder": f.placeholder or "", "help": f.help,
            }
            for f in m.stream_fields if f.name != "name"
        ]
        cards.append({
            "kind": "plugin",
            "plugin_id": m.plugin_id,
            "display_name": m.display_name,
            "help": m.help,
            "source_category": getattr(m, "source_category", "custom_source"),
            "content_types": list(getattr(m, "content_types", ["user_feedback"])),
            "configured": instance is not None,
            "connection_paused": conn_paused,
            "instance": instance or {
                "id": m.plugin_id, "paused": False,
                "credibility_weight": m.credibility_weight_default,
            },
            "streams": streams,
            "stream_fields": stream_fields,
            "identifier_field": m.identifier_field,
        })

    # Add one card per media catalog entry.
    rss_conn_paused = "rss" in globally_paused
    for entry_card in _media.catalog_cards(product):
        cards.append({
            "kind": "catalog_entry",
            "plugin_id": "rss",
            "display_name": entry_card["display_name"],
            "help": entry_card.get("domain") or "",
            "source_category": entry_card["source_category"],
            "content_types": entry_card["content_types"],
            "configured": entry_card["enabled"],
            "connection_paused": rss_conn_paused,
            "feed_url": entry_card["feed_url"],
            "instance": {
                "id": entry_card["instance_id"], "paused": entry_card["instance_paused"],
                "credibility_weight": 1.0,
            },
            # Catalog cards don't have per-stream config surface — the whole
            # card IS one stream (identity = feed_url). We still expose its
            # pause state via a single virtual stream row.
            "stream_paused": entry_card["stream_paused"],
        })
    return cards


def _group_cards_by_taxonomy(cards: list[dict]) -> list[dict]:
    """Group cards into [content_type][source_category] sections.

    A card tagged with multiple content_types appears in each. Section
    ordering: user_feedback first, then media_coverage. Sub-section
    ordering: rss_feed → custom_source → third_party_scraper.
    """
    top_order = ["user_feedback", "media_coverage"]
    sub_order = ["rss_feed", "custom_source", "third_party_scraper"]
    top_titles = {
        "user_feedback": "User Feedback Sources",
        "media_coverage": "Media Coverage Sources",
    }
    sub_titles = {
        "rss_feed": "RSS Feeds",
        "custom_source": "Custom Sources",
        "third_party_scraper": "Third-party Scrapers",
    }
    sections: list[dict] = []
    for ct in top_order:
        subs: list[dict] = []
        for sc in sub_order:
            matching = [c for c in cards
                        if ct in c.get("content_types", [])
                        and c.get("source_category") == sc]
            if not matching:
                continue
            # Bundle all catalog cards into a single "master panel" so the UI
            # can show them as one grouped chooser (Select all / Select none)
            # instead of 16 individual cards that require per-site clicks.
            plugin_cards = [c for c in matching if c.get("kind") != "catalog_entry"]
            catalog_cards = [c for c in matching if c.get("kind") == "catalog_entry"]
            plugin_cards.sort(key=lambda c: (0 if c.get("configured") else 1,
                                              c.get("display_name", "").lower()))
            catalog_cards.sort(key=lambda c: c.get("display_name", "").lower())
            subs.append({
                "source_category": sc,
                "title": sub_titles[sc],
                "cards": plugin_cards,
                "catalog_cards": catalog_cards,
                "catalog_enabled_count": sum(1 for c in catalog_cards if c.get("configured")),
                "catalog_total": len(catalog_cards),
            })
        if subs:
            sections.append({
                "content_type": ct,
                "title": top_titles[ct],
                "subsections": subs,
            })
    return sections


def _configured_summary(cards: list[dict]) -> list[dict]:
    """Compact table rows for the top 'Configured sources' section.

    One row per configured entity:
      - Each configured plugin card → one row (stream_count = # of streams)
      - All catalog entries collapse into a single 'Media Coverage Sources'
        row when at least one is enabled (stream_count = 'N of M enabled')

    Non-configured plugins are hidden — they live behind '+ Add more sources'.
    """
    rows: list[dict] = []
    catalog_enabled = 0
    catalog_total = 0
    catalog_paused = False
    catalog_conn_paused = False
    for c in cards:
        if c.get("kind") == "catalog_entry":
            catalog_total += 1
            if c.get("configured"):
                catalog_enabled += 1
            catalog_paused = catalog_paused or bool((c.get("instance") or {}).get("paused"))
            catalog_conn_paused = catalog_conn_paused or bool(c.get("connection_paused"))
            continue
        if not c.get("configured"):
            continue
        instance = c.get("instance") or {}
        rows.append({
            "kind": "plugin",
            "plugin_id": c["plugin_id"],
            "display_name": c["display_name"],
            "content_types": list(c.get("content_types") or []),
            "source_category": c.get("source_category", "custom_source"),
            "stream_count": len(c.get("streams") or []),
            "stream_count_label": str(len(c.get("streams") or [])),
            "instance_paused": bool(instance.get("paused")),
            "connection_paused": bool(c.get("connection_paused")),
        })
    if catalog_enabled > 0:
        rows.append({
            "kind": "catalog",
            "plugin_id": "rss",
            "display_name": "Media Coverage Sources",
            "content_types": ["media_coverage"],
            "source_category": "rss_feed",
            "stream_count": catalog_enabled,
            "stream_count_label": f"{catalog_enabled} of {catalog_total} enabled",
            "instance_paused": catalog_paused,
            "connection_paused": catalog_conn_paused,
        })
    rows.sort(key=lambda r: r["display_name"].lower())
    return rows


@app.get("/products/{product_id}/sources", response_class=HTMLResponse)
def sources_form(request: Request, product_id: str,
                  media_enabled: Optional[str] = None):
    product = _product_or_404(product_id)
    from pipeline import connections as _conn
    from pipeline import media_sources as _media

    globally_paused = _conn.paused_types()
    cards = _build_source_cards(product, globally_paused)
    sections = _group_cards_by_taxonomy(cards)
    configured_summary = _configured_summary(cards)
    # Sub-sections for the picker modal — only plugins/catalogs NOT yet on
    # the configured table. Catalog is treated as ONE virtual pickable when
    # at least one entry is unenabled (so users can add more publications
    # to an already-configured Media Coverage row).
    return templates.TemplateResponse(
        "sources_form.html",
        {
            "request": request,
            "product": product,
            "cards": cards,       # flat list retained for legacy references
            "sections": sections,
            "configured_summary": configured_summary,
            "media_status": _media.status_for_product(product_id),
            "media_flash": media_enabled,
        },
    )


@app.post("/products/{product_id}/media-coverage/enable-all")
def product_enable_media_coverage(product_id: str):
    """Append every missing media coverage feed URL to the product's
    sources.yaml as `type: rss` entries. Idempotent — already-present
    feeds (matched by URL) are skipped. See
    `pipeline/media_sources.py::enable_all_for_product` for details.

    This is the Option A path from the "does media coverage auto-fetch?"
    UX conversation — no auto-fetch, but one click on this button opts a
    product in.
    """
    _product_or_404(product_id)
    from pipeline import media_sources as _media
    result = _media.enable_all_for_product(product_id)
    if result["added"]:
        note = f"added+{len(result['added'])}+feed(s)"
        if result["already_present"]:
            note += f"+({len(result['already_present'])}+already+present)"
    elif result["already_present"]:
        note = f"all+{len(result['already_present'])}+feeds+already+enabled"
    else:
        note = "no+media+feeds+in+catalog"
    return RedirectResponse(
        url=f"/products/{product_id}/sources?media_enabled={note}",
        status_code=303,
    )


@app.post("/products/{product_id}/sources")
def sources_save(product_id: str, payload: dict = Body(...)):
    product_dir = _product_dir_for(product_id)
    sources_in = payload.get("sources") or []
    errors: list[str] = []
    cleaned: list[dict] = []
    seen_ids: set[str] = set()

    def _coerce(field: dict, raw: Any) -> Any:
        t = field["type"]
        if raw is None:
            raw = ""
        if t == "number":
            if isinstance(raw, str):
                raw = raw.strip()
            if raw == "" or raw is None:
                return field.get("default")
            try:
                v = float(raw)
                return int(v) if v.is_integer() else v
            except (TypeError, ValueError):
                return None
        if t == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in ("true", "1", "on", "yes")
        if t == "csv":
            if isinstance(raw, list):
                return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
            return [s.strip() for s in str(raw).split(",") if s.strip()]
        if t == "textarea_list":
            if isinstance(raw, list):
                return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
            return [s.strip() for s in str(raw).splitlines() if s.strip()]
        # text
        return str(raw).strip()

    for si, src in enumerate(sources_in):
        stype = (src.get("type") or "").strip()
        sid = (src.get("id") or "").strip().lower().replace(" ", "-")
        if not sid:
            errors.append(f"source #{si+1}: id is required")
            continue
        if sid in seen_ids:
            errors.append(f"source '{sid}' (#{si+1}): duplicate id")
            continue
        seen_ids.add(sid)
        if stype not in SOURCE_TYPE_META:
            errors.append(f"source '{sid}': unknown type {stype!r}")
            continue

        try:
            cred = float(src.get("credibility_weight", 1.0) or 1.0)
        except (TypeError, ValueError):
            errors.append(f"source '{sid}': credibility_weight must be a number")
            continue

        streams_in = src.get("streams") or []
        if not streams_in:
            errors.append(f"source '{sid}': at least one stream is required")
            continue

        fields = SOURCE_TYPE_META[stype]["stream_fields"]
        cleaned_streams: list[dict] = []
        for sti, stream in enumerate(streams_in):
            clean_stream: dict = {}
            # Per-stream pause is an explicit field, not one of the
            # type-specific stream_fields. Preserve it unconditionally.
            if bool(stream.get("paused")):
                clean_stream["paused"] = True
            for field in fields:
                value = _coerce(field, stream.get(field["name"]))
                if field.get("required") and not value and value != 0 and value is not False:
                    errors.append(
                        f"source '{sid}' stream #{sti+1}: '{field['label']}' is required"
                    )
                if value is None or value == "" or value == []:
                    continue
                clean_stream[field["name"]] = value
            # reddit_rss: normalize `subreddit` to a bare name (strip r/,
            # URLs, /new.rss, etc.) so the fetch code has a clean input and
            # sources.yaml on disk stays uniform.
            if stype == "reddit_rss" and "subreddit" in clean_stream:
                from sources.reddit_rss import normalize_subreddit
                raw_sub = clean_stream["subreddit"]
                bare = normalize_subreddit(raw_sub)
                if not bare:
                    errors.append(
                        f"source '{sid}' stream #{sti+1}: subreddit "
                        f"{raw_sub!r} isn't a valid subreddit — use r/<name>"
                    )
                else:
                    clean_stream["subreddit"] = bare
            cleaned_streams.append(clean_stream)

        cleaned.append({
            "id": sid,
            "type": stype,
            # Product-level pause. Runs skip this source instance until
            # unpaused. Superseded by the global pause on /connections.
            "paused": bool(src.get("paused")),
            "credibility_weight": cred,
            "streams": cleaned_streams,
        })

    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})

    sources_path = product_dir / "sources.yaml"
    backup_path = sources_path.with_suffix(".yaml.bak")
    if sources_path.exists():
        sources_path.replace(backup_path)
    try:
        sources_path.write_text(
            yaml.safe_dump({"sources": cleaned}, sort_keys=False, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        clear_cache()
        load_product(product_id)
    except Exception as e:
        if sources_path.exists():
            sources_path.unlink()
        if backup_path.exists():
            backup_path.replace(sources_path)
        clear_cache()
        raise HTTPException(status_code=422, detail={"errors": [str(e)]})
    if backup_path.exists():
        backup_path.unlink()
    return {"ok": True, "count": len(cleaned)}


# --- Prompts form (Phase 6) -------------------------------------------------
#
# Form-based editor for the relevance + classify LLM prompts. Each stage
# has its own group of fields: system message, user-prompt template, few-shot
# config, and (classify-only) extras instructions. The available template
# placeholders are listed on the right as a reference panel.

PROMPT_PLACEHOLDERS = {
    "relevance": [
        ("{product_display}", "Display name of the product (e.g., 'Microsoft Windows')."),
        ("{product_description}", "Description from product.yaml."),
        ("{title}", "Item's title (post title, issue title)."),
        ("{body}", "Item's body, truncated to 1000 chars."),
        ("{few_shot_block}", "Auto-rendered few-shot examples (when few_shot.enabled is true and the product has snippets)."),
    ],
    "classify": [
        ("{areas}", "Multi-line list of enabled areas (id: display) for the LLM to pick from."),
        ("{features}", "Hierarchical block: for each area, the features under it with each feature's description (the LLM-recognition prompt you wrote in the taxonomy editor). Use this to give the classifier the per-feature definitions you authored. Truncated to ~200 chars per feature so a 25-feature product stays under ~6KB."),
        ("{content_types}", "Comma-separated content-type vocabulary."),
        ("{extras_instructions}", "Free-form per-product notes (from the field below)."),
        ("{few_shot_block}", "Auto-rendered few-shot examples (when few_shot.enabled is true and the product has snippets)."),
        ("{kb_numbers}", "Comma list of KB numbers matched by regex."),
        ("{build_numbers}", "Comma list of Windows-build-style numbers matched by regex."),
        ("{parent_block}", "For comments: the parent post title + body excerpt (auto-filled)."),
        ("{title}", "Item's title."),
        ("{body}", "Item's body, truncated to 4000 chars."),
        ("{engagement}", "Item engagement metrics JSON."),
        ("{source}", "Display name of the source instance."),
    ],
}


@app.get("/products/{product_id}/prompts", response_class=HTMLResponse)
def prompts_form(request: Request, product_id: str, saved: Optional[str] = None, error: Optional[str] = None):
    product = _product_or_404(product_id)
    prompts = product.prompts or {}
    rel = prompts.get("relevance") or {}
    cls = prompts.get("classify") or {}
    return templates.TemplateResponse(
        "prompts_form.html",
        {
            "request": request,
            "product": product,
            "saved": saved,
            "error": error,
            "placeholders": PROMPT_PLACEHOLDERS,
            "values": {
                "relevance": {
                    "system": rel.get("system", "").rstrip("\n"),
                    "template": rel.get("template", "").rstrip("\n"),
                    "few_shot_enabled": bool((rel.get("few_shot") or {}).get("enabled", False)),
                    "few_shot_n_positive": int((rel.get("few_shot") or {}).get("n_positive", 3)),
                    "few_shot_n_negative": int((rel.get("few_shot") or {}).get("n_negative", 2)),
                },
                "classify": {
                    "system": cls.get("system", "").rstrip("\n"),
                    "template": cls.get("template", "").rstrip("\n"),
                    "extras_instructions": cls.get("extras_instructions", "").rstrip("\n"),
                    "few_shot_enabled": bool((cls.get("few_shot") or {}).get("enabled", False)),
                    "few_shot_n_positive": int((cls.get("few_shot") or {}).get("n_positive", 2)),
                    "few_shot_n_negative": int((cls.get("few_shot") or {}).get("n_negative", 1)),
                },
            },
        },
    )


@app.post("/products/{product_id}/prompts")
async def prompts_save(product_id: str, request: Request):
    product_dir = _product_dir_for(product_id)
    form = await request.form()

    def _int(name: str, default: int) -> int:
        try:
            return int(form.get(name) or default)
        except (TypeError, ValueError):
            return default

    new_doc = {
        "relevance": {
            "system": (form.get("relevance.system") or "").rstrip("\n") + "\n",
            "few_shot": {
                "enabled": form.get("relevance.few_shot_enabled") == "on",
                "n_positive": _int("relevance.few_shot_n_positive", 3),
                "n_negative": _int("relevance.few_shot_n_negative", 2),
            },
            "template": (form.get("relevance.template") or "").rstrip("\n") + "\n",
        },
        "classify": {
            "system": (form.get("classify.system") or "").rstrip("\n") + "\n",
            "extras_instructions": (form.get("classify.extras_instructions") or "").rstrip("\n"),
            "few_shot": {
                "enabled": form.get("classify.few_shot_enabled") == "on",
                "n_positive": _int("classify.few_shot_n_positive", 2),
                "n_negative": _int("classify.few_shot_n_negative", 1),
            },
            "template": (form.get("classify.template") or "").rstrip("\n") + "\n",
        },
    }

    # Cheap pre-check: the templates must at least be non-empty.
    errors = []
    if not new_doc["relevance"]["template"].strip():
        errors.append("relevance.template is required (it's the user-prompt the LLM sees).")
    if not new_doc["classify"]["template"].strip():
        errors.append("classify.template is required.")
    if errors:
        return RedirectResponse(
            url=f"/products/{product_id}/prompts?error=" + " | ".join(errors)[:300],
            status_code=303,
        )

    prompts_path = product_dir / "prompts.yaml"
    backup_path = prompts_path.with_suffix(".yaml.bak")
    if prompts_path.exists():
        prompts_path.replace(backup_path)
    try:
        prompts_path.write_text(
            yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        clear_cache()
        load_product(product_id)
    except Exception as e:
        if prompts_path.exists():
            prompts_path.unlink()
        if backup_path.exists():
            backup_path.replace(prompts_path)
        clear_cache()
        return RedirectResponse(
            url=f"/products/{product_id}/prompts?error={str(e)[:200]}",
            status_code=303,
        )
    if backup_path.exists():
        backup_path.unlink()
    # POST_V1_PLAN §4.5 — bump version + archive on every explicit save so
    # history/A-B testing works from day one.
    try:
        from pipeline import prompt_versioning as _pv
        _pv.bump_version(product_dir, new_doc)
    except Exception:
        pass  # versioning failure never blocks the save
    return RedirectResponse(url=f"/products/{product_id}/prompts?saved=1", status_code=303)


# --- Digest v2 report config (report_v2_design.md §7.4) --------------------
#
# Per-product editor for products/<id>/report_config.yaml. Section toggles,
# sentiment thresholds, headline top-N. Competitor list is edited on the
# product profile page (it's a product fact, not a report setting).


@app.get("/products/{product_id}/report", response_class=HTMLResponse)
def report_config_form(request: Request, product_id: str,
                       saved: Optional[str] = None, error: Optional[str] = None):
    from pipeline import features as _features
    from pipeline import report_config as _rc
    product = _product_or_404(product_id)
    if not _features.enabled("digest_v2_enabled", product_id):
        return templates.TemplateResponse(
            "base.html",
            {
                "request": request,
                "content": (
                    "<h2>Digest v2 not enabled</h2><p>Flip "
                    "<code>digest_v2_enabled: true</code> in "
                    "<code>config/features.yaml</code> to configure the digest.</p>"
                ),
            },
        )
    return templates.TemplateResponse(
        "product_report.html",
        {
            "request": request,
            "product": product,
            "cfg": _rc.load(product_id),
            "config_path": str(_rc.path_for(product_id)),
            "saved": saved,
            "error": error,
        },
    )


@app.post("/products/{product_id}/report")
async def report_config_save(product_id: str, request: Request):
    from pipeline import report_config as _rc
    _product_or_404(product_id)
    form = await request.form()

    def _float(name: str, default: float) -> float:
        try:
            return float(form.get(name) or default)
        except (TypeError, ValueError):
            return default

    def _int(name: str, default: int) -> int:
        try:
            return int(form.get(name) or default)
        except (TypeError, ValueError):
            return default

    new_doc = {
        "digest_v2": {
            "sections": {
                "positive": form.get("sections.positive") == "on",
                "negative": form.get("sections.negative") == "on",
                "bugs": form.get("sections.bugs") == "on",
                "features": form.get("sections.features") == "on",
                "competition": form.get("sections.competition") == "on",
            },
            "sentiment_thresholds": {
                "positive": _float("thresholds.positive", 0.2),
                "negative": _float("thresholds.negative", -0.2),
            },
            "headline_top_n": _int("headline_top_n", 25),
        },
    }

    # Validate thresholds are sane before writing.
    if new_doc["digest_v2"]["sentiment_thresholds"]["positive"] <= \
       new_doc["digest_v2"]["sentiment_thresholds"]["negative"]:
        return RedirectResponse(
            url=f"/products/{product_id}/report?error=positive+threshold+must+be+greater+than+negative",
            status_code=303,
        )

    path = _rc.path_for(product_id)
    backup = path.with_suffix(".yaml.bak")
    if path.exists():
        path.replace(backup)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True,
                           default_flow_style=False),
            encoding="utf-8",
        )
    except Exception as e:
        if path.exists():
            path.unlink()
        if backup.exists():
            backup.replace(path)
        return RedirectResponse(
            url=f"/products/{product_id}/report?error={str(e)[:200]}",
            status_code=303,
        )
    if backup.exists():
        backup.unlink()
    return RedirectResponse(url=f"/products/{product_id}/report?saved=1", status_code=303)


# --- Prompt suggestions (POST_V1_PLAN §4.5) ---------------------------------
#
# Assistant LLM analyzes recent snippets + current prompt and proposes a
# small ordered list of edits. Each suggestion is coverage-checked against
# the last 30 days of items; edits that would drop relevance pass rate by
# > 10pp are flagged red and blocked.
#
# Feature-flagged by `prompt_suggestions_enabled`. Depends on the
# assistant LLM being configured.


@app.get("/products/{product_id}/prompts/suggestions", response_class=HTMLResponse)
def prompts_suggestions(request: Request, product_id: str):
    from pipeline import features as _features
    product = _product_or_404(product_id)
    if not _features.enabled("prompt_suggestions_enabled", product_id):
        return templates.TemplateResponse(
            "prompt_suggestions.html",
            {"request": request, "product": product, "flag_off": True,
             "suggestions": None, "coverage_results": [], "error": None},
        )

    from pipeline import prompt_suggestions as _ps
    current_prompts = dict(product.prompts or {})
    # Recent snippets = added AFTER the current prompt was last updated.
    # For a first implementation, use the last 20 snippets ordered by
    # created_at desc (§4.4 snippets carry created_at).
    recent = sorted(
        (s for s in product.snippets if s.created_at is not None),
        key=lambda s: s.created_at, reverse=True,
    )[:20]

    if not recent:
        return templates.TemplateResponse(
            "prompt_suggestions.html",
            {"request": request, "product": product, "flag_off": False,
             "suggestions": None, "coverage_results": [], "recent_count": 0,
             "error": ("No snippets to review yet. Add positive/negative "
                       "examples on the Snippets page first.")},
        )

    result = _ps.generate_suggestions(
        current_prompts=current_prompts,
        recent_snippets=recent,
    )
    if result is None:
        return templates.TemplateResponse(
            "prompt_suggestions.html",
            {"request": request, "product": product, "flag_off": False,
             "suggestions": None, "coverage_results": [], "recent_count": len(recent),
             "error": ("Assistant LLM is unavailable or returned no suggestions. "
                       "Check /connections/assistant_llm.")},
        )

    # Per-edit coverage check
    coverage_results = []
    for edit in result.edits:
        try:
            proposed = _ps.apply_edit(current_prompts, edit)
        except ValueError as e:
            coverage_results.append({"error": str(e), "coverage": None})
            continue
        cov = _ps.coverage_check(
            product_id=product_id,
            baseline_prompts=current_prompts,
            proposed_prompts=proposed,
        )
        coverage_results.append({"error": None, "coverage": cov})

    return templates.TemplateResponse(
        "prompt_suggestions.html",
        {"request": request, "product": product, "flag_off": False,
         "suggestions": result, "coverage_results": coverage_results,
         "recent_count": len(recent), "error": None},
    )


@app.post("/products/{product_id}/prompts/suggestions/apply")
async def prompts_suggestions_apply(product_id: str, request: Request):
    """Apply one or more selected edits to prompts.yaml + archive the
    prior version. Each posted `edit_<i>_kind`/`_target`/`_before`/`_after`
    describes one edit; the checkbox `apply_<i>` selects which to keep."""
    from pipeline import features as _features
    if not _features.enabled("prompt_suggestions_enabled", product_id):
        raise HTTPException(status_code=403, detail="prompt_suggestions is disabled")

    product = _product_or_404(product_id)
    product_dir = _product_dir_for(product_id)
    from pipeline import prompt_suggestions as _ps
    from pipeline import prompt_versioning as _pv
    from pipeline.prompt_suggestions import PromptEdit

    form = dict(await request.form())
    selected: list[PromptEdit] = []
    i = 0
    while True:
        kind_key = f"edit_{i}_kind"
        if kind_key not in form:
            break
        if form.get(f"apply_{i}") == "on":
            try:
                selected.append(PromptEdit(
                    kind=form[kind_key],
                    target=form.get(f"edit_{i}_target", ""),
                    before=form.get(f"edit_{i}_before", ""),
                    after=form.get(f"edit_{i}_after", ""),
                    rationale=form.get(f"edit_{i}_rationale", ""),
                ))
            except Exception as e:
                return RedirectResponse(
                    url=f"/products/{product_id}/prompts?error=invalid+edit+{i}:+{str(e)[:120]}",
                    status_code=303,
                )
        i += 1

    if not selected:
        return RedirectResponse(
            url=f"/products/{product_id}/prompts?error=no+edits+selected",
            status_code=303,
        )

    current = dict(product.prompts or {})
    try:
        for e in selected:
            current = _ps.apply_edit(current, e)
    except ValueError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/prompts?error={str(e)[:200]}",
            status_code=303,
        )

    _pv.bump_version(product_dir, current)
    clear_cache()
    load_product(product_id)
    return RedirectResponse(
        url=f"/products/{product_id}/prompts?saved=1",
        status_code=303,
    )


# --- Taxonomy form (Phase 3) ------------------------------------------------
#
# Form-based editor for the Product -> Area -> Feature hierarchy. The user
# adds / edits / removes areas and the features under each, plus per-area
# keywords + entity_type_hint. Features are the leaf with display + description
# (description is the prompt that tells the LLM what to look for).
#
# Save flow: client serializes the tree to JSON, POSTs to
# /products/{id}/taxonomy. Server validates (>=1 feature per area, unique
# ids), rewrites taxonomy.yaml, bumps `version` to today, clears cache,
# returns {ok: true} or 422 with details.


@app.get("/products/{product_id}/taxonomy", response_class=HTMLResponse)
def taxonomy_form(request: Request, product_id: str):
    product = _product_or_404(product_id)
    areas = product.taxonomy.get("areas") or []
    return templates.TemplateResponse(
        "taxonomy_form.html",
        {
            "request": request,
            "product": product,
            "areas": areas,
            "version": product.taxonomy_version,
        },
    )


@app.post("/products/{product_id}/taxonomy")
def taxonomy_save(product_id: str, payload: dict = Body(...)):
    product_dir = _product_dir_for(product_id)
    areas_in = payload.get("areas") or []

    # Validate.
    errors: list[str] = []
    seen_area_ids: set[str] = set()
    cleaned_areas: list[dict] = []
    for ai, a in enumerate(areas_in):
        aid = (a.get("id") or "").strip().lower().replace(" ", "-")
        adisplay = (a.get("display") or "").strip()
        if not aid:
            errors.append(f"area {ai+1}: id is required")
            continue
        if aid in seen_area_ids:
            errors.append(f"area '{aid}' (#{ai+1}): duplicate id")
            continue
        seen_area_ids.add(aid)
        if not adisplay:
            errors.append(f"area '{aid}': display name is required")
            continue
        feats_in = a.get("features") or []
        if not feats_in:
            errors.append(f"area '{aid}': at least one feature is required")
            continue
        seen_feat_ids: set[str] = set()
        cleaned_feats: list[dict] = []
        for fi, f in enumerate(feats_in):
            fid = (f.get("id") or "").strip().lower().replace(" ", "-")
            fdisplay = (f.get("display") or "").strip()
            fdesc = (f.get("description") or "").strip()
            if not fid:
                errors.append(f"area '{aid}' feature {fi+1}: id is required")
                continue
            if fid in seen_feat_ids:
                errors.append(f"area '{aid}' feature '{fid}': duplicate id within area")
                continue
            seen_feat_ids.add(fid)
            if not fdisplay:
                errors.append(f"area '{aid}' feature '{fid}': display name is required")
                continue
            if not fdesc:
                errors.append(f"area '{aid}' feature '{fid}': description is required (it's the LLM prompt)")
                continue
            cleaned_feats.append({"id": fid, "display": fdisplay, "description": fdesc})

        def _split_list(raw: Any) -> list[str]:
            if isinstance(raw, list):
                return [s.strip() for s in raw if isinstance(s, str) and s.strip()]
            if isinstance(raw, str):
                return [s.strip() for s in raw.split(",") if s.strip()]
            return []

        cleaned_areas.append({
            "id": aid,
            "display": adisplay,
            "enabled": bool(a.get("enabled", True)),
            "keywords": _split_list(a.get("keywords")),
            "entity_type_hint": _split_list(a.get("entity_type_hint")),
            "features": cleaned_feats,
        })

    if errors:
        raise HTTPException(status_code=422, detail={"errors": errors})
    if not cleaned_areas:
        raise HTTPException(status_code=422, detail={"errors": ["at least one area is required"]})

    # Write back to taxonomy.yaml. Bump version to today so trend continuity
    # markers show a discontinuity.
    new_doc = {
        "version": date.today().isoformat(),
        "areas": cleaned_areas,
    }
    taxonomy_path = product_dir / "taxonomy.yaml"
    backup_path = taxonomy_path.with_suffix(".yaml.bak")
    if taxonomy_path.exists():
        taxonomy_path.replace(backup_path)
    try:
        taxonomy_path.write_text(
            yaml.safe_dump(new_doc, sort_keys=False, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
        clear_cache()
        # Validate by reloading.
        load_product(product_id)
    except Exception as e:
        # Roll back.
        if taxonomy_path.exists():
            taxonomy_path.unlink()
        if backup_path.exists():
            backup_path.replace(taxonomy_path)
        clear_cache()
        raise HTTPException(status_code=422, detail={"errors": [str(e)]})
    if backup_path.exists():
        backup_path.unlink()
    return {"ok": True, "version": new_doc["version"]}


# --- YAML editors (UI 2) ----------------------------------------------------
#
# Each per-product YAML file (sources.yaml, prompts.yaml, taxonomy.yaml,
# llm_routing.yaml) has the same shape of editor:
#
#   GET  /products/{id}/<thing>          render YAML in a textarea
#   POST /products/{id}/<thing>          parse + validate (via product re-load),
#                                        write file on success, redirect back
#
# Validation strategy: write to a temp file, attempt to YAML-parse it, attempt
# to re-load the product with the new content swapped in (catches schema-level
# issues for sources/prompts/etc.), commit on success.

_EDITORS = {
    "sources": {
        "filename": "sources.yaml",
        "title": "Sources",
        "help": "Source instances and their per-stream config. `type` must match a registered plugin.",
    },
    "prompts": {
        "filename": "prompts.yaml",
        "title": "Prompts",
        "help": "Relevance + classify prompt templates. Placeholders: {product_display}, {title}, {body}, {areas}, {content_types}, {few_shot_block}, {kb_numbers}, {build_numbers}, {parent_block}, {extras_instructions}.",
    },
    "taxonomy": {
        "filename": "taxonomy.yaml",
        "title": "Taxonomy",
        "help": "Functional areas. Bump `version` when you edit so trend charts can mark a discontinuity.",
    },
    "llm_routing": {
        "filename": "llm_routing.yaml",
        "title": "LLM routing",
        "help": "Per-stage adapter config (endpoint, model, temperature, seed).",
    },
}


def _product_dir_for(product_id: str) -> Path:
    d = PRODUCTS_DIR / product_id
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"product '{product_id}' not found")
    return d


@app.get("/products/{product_id}/edit/{section}", response_class=HTMLResponse)
def yaml_editor(request: Request, product_id: str, section: str, error: Optional[str] = None):
    if section not in _EDITORS:
        raise HTTPException(status_code=404, detail=f"unknown section: {section}")
    meta = _EDITORS[section]
    product_dir = _product_dir_for(product_id)
    file_path = product_dir / meta["filename"]
    body = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    return templates.TemplateResponse(
        "yaml_editor.html",
        {
            "request": request,
            "product_id": product_id,
            "section": section,
            "title": meta["title"],
            "filename": meta["filename"],
            "help": meta["help"],
            "body": body,
            "error": error,
        },
    )


@app.post("/products/{product_id}/edit/{section}")
def yaml_editor_save(product_id: str, section: str, body: str = Form(...)):
    if section not in _EDITORS:
        raise HTTPException(status_code=404, detail=f"unknown section: {section}")
    meta = _EDITORS[section]
    product_dir = _product_dir_for(product_id)
    file_path = product_dir / meta["filename"]

    # 1. Parse YAML — surface syntax errors back to the editor.
    try:
        yaml.safe_load(body)
    except yaml.YAMLError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/edit/{section}?error=YAML+parse+error:+{str(e)[:120]}",
            status_code=303,
        )

    # 2. Write atomically (write to tmp, swap).
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")

    # 3. Reload-validate. If load_product raises, roll back.
    clear_cache()
    backup = None
    if file_path.exists():
        backup = file_path.with_suffix(file_path.suffix + ".bak")
        file_path.replace(backup)
    tmp.replace(file_path)
    try:
        load_product(product_id)
    except Exception as e:
        # Roll back.
        file_path.unlink(missing_ok=True)
        if backup is not None:
            backup.replace(file_path)
        clear_cache()
        msg = str(e)[:150].replace("+", " ")
        return RedirectResponse(
            url=f"/products/{product_id}/edit/{section}?error=Validation+failed:+{msg}",
            status_code=303,
        )

    if backup is not None and backup.exists():
        backup.unlink()
    return RedirectResponse(url=f"/products/{product_id}/edit/{section}?error=", status_code=303)



# --- Snippets (UI 3) --------------------------------------------------------


def _product_or_404(product_id: str):
    try:
        p = load_product(product_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # Set as current so downstream storage.warehouse() / current_product()
    # resolve paths to this product's warehouse.duckdb rather than whichever
    # product a prior request set. Without this, per-request calls into
    # pipeline modules that use the "current product" indirection (e.g.
    # token_usage.per_run_totals, engagement.distribution_by_source) open
    # the wrong DB.
    set_current_product(p)
    return p


def _snippets_index_url(product_id: str) -> str:
    return f"/products/{product_id}/snippets"


@app.get("/products/{product_id}/snippets", response_class=HTMLResponse)
def snippets_list(request: Request, product_id: str):
    product = _product_or_404(product_id)
    snips = sorted(product.snippets, key=lambda s: (not s.is_positive, s.id))
    return templates.TemplateResponse(
        "snippets_list.html",
        {"request": request, "product": product, "snippets": snips},
    )


@app.get("/products/{product_id}/snippets/new", response_class=HTMLResponse)
def snippets_new_form(request: Request, product_id: str, mode: str = "url"):
    product = _product_or_404(product_id)
    if mode not in ("url", "text"):
        mode = "url"
    return templates.TemplateResponse(
        "snippet_form.html",
        {
            "request": request,
            "product": product,
            "mode": mode,
            "snippet": None,                 # new
            "area_ids": product.area_ids(),
            "content_types": sorted(CONTENT_TYPES),
            "severity_values": ["", *sorted(SEVERITY_VALUES)],
            "form_action": f"/products/{product_id}/snippets",
            "edit": False,
            "error": None,
        },
    )


# Route order matters: /snippets/candidates* must be registered BEFORE
# /snippets/{snippet_id} so the parameterised route doesn't swallow it.
# The real handlers are defined further down; here we just forward.

@app.get("/products/{product_id}/snippets/candidates", response_class=HTMLResponse)
def _snippets_candidates_route(
    request: Request,
    product_id: str,
    nonce: Optional[str] = None,
    idx: int = 0,
    error: Optional[str] = None,
):
    return snippets_candidates(request, product_id, nonce, idx, error)


@app.post("/products/{product_id}/snippets/candidates/decide")
async def _snippets_candidates_decide_route(product_id: str, request: Request):
    return await snippets_candidates_decide(product_id, request)


# --- Calibrate-more: reuse wizard mini-fetch on a LIVE product -----------------
#
# Fetch ~50 items from selected sources, judge them relevant / not relevant,
# then flush judgments as snippet YAML files. Reuses pipeline/minifetch.py and
# webui/templates/wizard/step_calibrate.html (with calibrate_more_mode=True).
# State lives at data/.wizard/product-<id>/ so it can't collide with wizard drafts.
#
# Route order: /snippets/calibrate* MUST come before /snippets/{snippet_id}
# so the parameterised GET/POST don't swallow it.


def _product_as_calibrate_draft(product):
    """Minimal draft-shaped object for step_calibrate.html reuse. Only carries
    the fields the template actually reads (slug, display, calibration)."""
    from types import SimpleNamespace
    from pipeline import calibrate_more as _cm
    return SimpleNamespace(
        slug=product.id,
        display=product.display or product.id,
        calibration={"judgments": _cm.read_judgments(product.id)},
    )


def _available_calibrate_sources(product):
    """Sources shown on the calibrate picker — active (non-paused) sources with
    at least one stream. `stream_count` is displayed as a hint.

    product.sources are plain dicts (see pipeline.product.ProductSpec)."""
    out = []
    for src in (product.sources or []):
        if src.get("paused"):
            continue
        streams = src.get("streams") or []
        if not streams:
            continue
        out.append({
            "id": src.get("id"),
            "type": src.get("type"),
            "stream_count": len(streams),
        })
    return out


@app.get("/products/{product_id}/snippets/calibrate", response_class=HTMLResponse)
def snippets_calibrate_page(request: Request, product_id: str):
    from pipeline import calibrate_more as _cm
    from pipeline import minifetch as _mf
    product = _product_or_404(product_id)
    draft = _product_as_calibrate_draft(product)
    status = _cm.read_status(product_id)
    judged = draft.calibration["judgments"]
    deck = _cm.sample_deck(product_id, size=10) if status.status == _mf.STATUS_READY else []
    return templates.TemplateResponse(
        "wizard/step_calibrate.html",
        {
            "request": request,
            "draft": draft,
            "calibrate_more_mode": True,
            "minifetch_status": status,
            "deck": deck,
            "n_judged": len(judged),
            "min_useful": _mf.MIN_USEFUL_ITEMS,
            "available_sources": _available_calibrate_sources(product),
        },
    )


@app.post("/products/{product_id}/snippets/calibrate/start")
async def snippets_calibrate_start(product_id: str, request: Request):
    from pipeline import calibrate_more as _cm
    product = _product_or_404(product_id)
    form = await request.form()
    selected = form.getlist("source_id")
    use_llm_gate = bool(form.get("use_llm_gate"))
    if not selected:
        # No sources picked and no defaults available — bounce back with a note.
        available = _available_calibrate_sources(product)
        if not available:
            return RedirectResponse(
                url=f"/products/{product_id}/snippets",
                status_code=303,
            )
        # User unchecked everything — treat as 'use all'.
        selected = [s["id"] for s in available]
    _cm.start(product, selected, use_llm_gate=use_llm_gate)
    return RedirectResponse(
        url=f"/products/{product_id}/snippets/calibrate",
        status_code=303,
    )


@app.post("/products/{product_id}/snippets/calibrate")
async def snippets_calibrate_judge(product_id: str, request: Request):
    from pipeline import calibrate_more as _cm
    _product_or_404(product_id)
    form = await request.form()
    item_id = (form.get("item_id") or "").strip()
    verdict = (form.get("verdict") or "").strip()
    if item_id and verdict in ("relevant", "not_relevant"):
        corpus_item = _cm.find_corpus_item(product_id, item_id) or {}
        _cm.record_judgment(product_id, item_id, verdict, corpus_item)
    # verdict='skip' falls through — no record, next GET drops it from the deck
    # once judged advances (skip stays visible until judged).
    return RedirectResponse(
        url=f"/products/{product_id}/snippets/calibrate",
        status_code=303,
    )


@app.post("/products/{product_id}/snippets/calibrate/done")
def snippets_calibrate_done(product_id: str):
    from pipeline import calibrate_more as _cm
    _product_or_404(product_id)
    product_dir = _product_dir_for(product_id)
    n_pos, n_neg = _cm.flush_to_snippets(product_id, product_dir)
    clear_cache()
    load_product(product_id)  # refresh in-memory product cache with new snippets
    return RedirectResponse(
        url=f"/products/{product_id}/snippets",
        status_code=303,
    )


@app.post("/products/{product_id}/snippets/calibrate/reset")
def snippets_calibrate_reset(product_id: str):
    from pipeline import calibrate_more as _cm
    _product_or_404(product_id)
    _cm.reset(product_id)
    return RedirectResponse(
        url=f"/products/{product_id}/snippets/calibrate",
        status_code=303,
    )


@app.get("/products/{product_id}/snippets/{snippet_id}", response_class=HTMLResponse)
def snippets_edit_form(request: Request, product_id: str, snippet_id: str, error: Optional[str] = None):
    product = _product_or_404(product_id)
    snip = next((s for s in product.snippets if s.id == snippet_id), None)
    if snip is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    mode = "url" if snip.source_url else "text"
    return templates.TemplateResponse(
        "snippet_form.html",
        {
            "request": request,
            "product": product,
            "mode": mode,
            "snippet": snip,
            "area_ids": product.area_ids(),
            "content_types": sorted(CONTENT_TYPES),
            "severity_values": ["", *sorted(SEVERITY_VALUES)],
            "form_action": f"/products/{product_id}/snippets/{snippet_id}",
            "edit": True,
            "error": error,
        },
    )


def _build_snippet_from_form(
    *,
    product,
    snippet_id: Optional[str],
    polarity: str,
    source_url: str,
    title: str,
    body: str,
    summary: str,
    is_topic_relevant: bool,
    areas: list[str],
    content_types_in: list[str],
    sentiment: Optional[float],
    bug_severity: str,
    notes: str,
    holdout_eval: bool,
) -> Snippet:
    if polarity not in (POSITIVE, NEGATIVE):
        raise ValueError(f"polarity must be positive_example or negative_example, got {polarity!r}")
    body = (body or "").strip()
    source_url = (source_url or "").strip() or None
    if not body and not source_url:
        raise ValueError("snippet needs either a URL or pasted body text")

    labels: dict = {"is_topic_relevant": bool(is_topic_relevant)}
    if areas:
        labels["areas"] = areas
    if content_types_in:
        labels["content_types"] = content_types_in
    if sentiment is not None:
        labels["sentiment"] = sentiment
    if summary:
        labels["summary"] = summary
    if bug_severity:
        labels["bug_severity"] = bug_severity

    # Default id from title or first words of body.
    sid = snippet_id or slugify(title or body[:60] or polarity)
    # Resolve collisions with a numeric suffix.
    existing_ids = {s.id for s in product.snippets}
    if not snippet_id and sid in existing_ids:
        base = sid
        n = 2
        while f"{base}-{n}" in existing_ids:
            n += 1
        sid = f"{base}-{n}"

    return Snippet(
        id=sid,
        polarity=polarity,
        source_url=source_url,
        title=title.strip() or None,
        body=body,
        labels=labels,
        holdout_eval=bool(holdout_eval),
        notes=notes.strip(),
    )


def _parse_areas(raw: list[str]) -> list[str]:
    return [a.strip() for a in raw if a and a.strip()]


@app.post("/products/{product_id}/snippets")
async def snippets_create(product_id: str, request: Request):
    product = _product_or_404(product_id)
    form = await request.form()
    try:
        snippet = _build_snippet_from_form(
            product=product,
            snippet_id=None,
            polarity=form.get("polarity") or POSITIVE,
            source_url=form.get("source_url") or "",
            title=form.get("title") or "",
            body=form.get("body") or "",
            summary=(form.get("summary") or "").strip(),
            is_topic_relevant=form.get("is_topic_relevant") == "on",
            areas=_parse_areas(form.getlist("areas")),
            content_types_in=_parse_areas(form.getlist("content_types")),
            sentiment=float(form["sentiment"]) if form.get("sentiment") else None,
            bug_severity=(form.get("bug_severity") or "").strip(),
            notes=form.get("notes") or "",
            holdout_eval=form.get("holdout_eval") == "on",
        )
    except ValueError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/snippets/new?mode={form.get('mode', 'url')}",
            status_code=303,
        )
    product_dir = PRODUCTS_DIR / product_id
    save_snippet(product_dir, snippet)
    clear_cache()
    return RedirectResponse(url=_snippets_index_url(product_id), status_code=303)


@app.post("/products/{product_id}/snippets/{snippet_id}")
async def snippets_update(product_id: str, snippet_id: str, request: Request):
    product = _product_or_404(product_id)
    existing = next((s for s in product.snippets if s.id == snippet_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    form = await request.form()
    try:
        new_snippet = _build_snippet_from_form(
            product=product,
            snippet_id=snippet_id,  # keep the same id
            polarity=form.get("polarity") or existing.polarity,
            source_url=form.get("source_url") or "",
            title=form.get("title") or "",
            body=form.get("body") or "",
            summary=(form.get("summary") or "").strip(),
            is_topic_relevant=form.get("is_topic_relevant") == "on",
            areas=_parse_areas(form.getlist("areas")),
            content_types_in=_parse_areas(form.getlist("content_types")),
            sentiment=float(form["sentiment"]) if form.get("sentiment") else None,
            bug_severity=(form.get("bug_severity") or "").strip(),
            notes=form.get("notes") or "",
            holdout_eval=form.get("holdout_eval") == "on",
        )
    except ValueError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/snippets/{snippet_id}?error={str(e)[:120]}",
            status_code=303,
        )

    # Polarity change moves the file across directories — delete the old one
    # (in its old polarity dir) before saving the new one.
    if existing.polarity != new_snippet.polarity:
        delete_snippet(existing)

    product_dir = PRODUCTS_DIR / product_id
    save_snippet(product_dir, new_snippet)
    clear_cache()
    return RedirectResponse(url=_snippets_index_url(product_id), status_code=303)


@app.post("/products/{product_id}/snippets/{snippet_id}/delete")
def snippets_delete(product_id: str, snippet_id: str):
    product = _product_or_404(product_id)
    existing = next((s for s in product.snippets if s.id == snippet_id), None)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"snippet '{snippet_id}' not found")
    delete_snippet(existing)
    clear_cache()
    return RedirectResponse(url=_snippets_index_url(product_id), status_code=303)


# --- Snippet candidates (POST_V1_PLAN §4.4-B) -------------------------------
#
# LLM-proposed candidates from the warehouse. Feature-flagged by
# `snippet_candidates_enabled`. Pool is generated once per session, cached
# to disk, then walked one-at-a-time with keyboard shortcuts (Y/N/S).


def _candidates_cache_path(product_id: str, nonce: str) -> Path:
    return _product_data_root(product_id) / "temp_runs" / f"candidates_{nonce}.json"


def _load_candidate_cache(product_id: str, nonce: str) -> Optional[dict]:
    path = _candidates_cache_path(product_id, nonce)
    if not path.exists():
        return None
    try:
        return _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def snippets_candidates(
    request: Request,
    product_id: str,
    nonce: Optional[str] = None,
    idx: int = 0,
    error: Optional[str] = None,
):
    from pipeline import features as _features
    product = _product_or_404(product_id)
    if not _features.enabled("snippet_candidates_enabled", product_id):
        return templates.TemplateResponse(
            "snippets_candidates.html",
            {
                "request": request, "product": product,
                "flag_off": True, "cache": None, "error": error,
            },
        )

    # No nonce → fresh pool: sample warehouse, ask assistant LLM, cache to disk.
    if not nonce:
        from pipeline import snippet_candidates as _sc
        pool = _sc.sample_candidate_pool(product_id)
        if not pool.items:
            return templates.TemplateResponse(
                "snippets_candidates.html",
                {
                    "request": request, "product": product,
                    "flag_off": False, "cache": None, "error": (
                        "No relevant items in the warehouse yet. Run the pipeline "
                        "to ingest and classify some data first."
                    ),
                },
            )
        zipped = _sc.rank_and_zip(pool)
        if not zipped:
            return templates.TemplateResponse(
                "snippets_candidates.html",
                {
                    "request": request, "product": product,
                    "flag_off": False, "cache": None, "error": (
                        "Assistant LLM produced no suggestions. Verify the assistant "
                        "LLM is configured at /connections/assistant_llm and within budget."
                    ),
                },
            )
        nonce = uuid.uuid4().hex[:12]
        cache = {
            "nonce": nonce,
            "product_id": product_id,
            "total_relevant": pool.total_relevant,
            "per_area_counts": pool.per_area_counts,
            "candidates": [
                {"item": z["item"], "suggestion":
                    z["suggestion"].model_dump() if z["suggestion"] else None,
                 "decided": None}
                for z in zipped if z["suggestion"] is not None    # only ranked
            ],
        }
        # Serialize datetimes to ISO before writing.
        for c in cache["candidates"]:
            ca = c["item"].get("created_at")
            if hasattr(ca, "isoformat"):
                c["item"]["created_at"] = ca.isoformat()
        path = _candidates_cache_path(product_id, nonce)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(cache), encoding="utf-8")
        return RedirectResponse(
            url=f"/products/{product_id}/snippets/candidates?nonce={nonce}&idx=0",
            status_code=303,
        )

    cache = _load_candidate_cache(product_id, nonce)
    if not cache:
        return RedirectResponse(
            url=f"/products/{product_id}/snippets/candidates",
            status_code=303,
        )
    total = len(cache.get("candidates") or [])
    idx = max(0, min(idx, total))
    return templates.TemplateResponse(
        "snippets_candidates.html",
        {
            "request": request, "product": product, "flag_off": False,
            "cache": cache, "idx": idx, "total": total, "error": error,
        },
    )


async def snippets_candidates_decide(product_id: str, request: Request):
    """Accept or skip a candidate. Creates a snippet with created_at=now
    when accepted (positive_example / negative_example) or just advances
    the cursor when skipped."""
    product = _product_or_404(product_id)
    form = await request.form()
    nonce = (form.get("nonce") or "").strip()
    try:
        idx = int(form.get("idx") or 0)
    except ValueError:
        idx = 0
    decision = (form.get("decision") or "skip").strip()

    cache = _load_candidate_cache(product_id, nonce)
    if not cache:
        return RedirectResponse(
            url=f"/products/{product_id}/snippets/candidates",
            status_code=303,
        )
    candidates = cache.get("candidates") or []
    if 0 <= idx < len(candidates):
        cand = candidates[idx]
        item = cand.get("item") or {}
        if decision in (POSITIVE, NEGATIVE):
            snippet = Snippet(
                id=slugify((item.get("title") or item.get("body") or "candidate")[:60]),
                polarity=decision,
                source_url=item.get("url") or None,
                title=item.get("title") or None,
                body=item.get("body") or "",
                labels={
                    "is_topic_relevant": (decision == POSITIVE),
                    "areas": [item["primary_area"]] if item.get("primary_area")
                              and item["primary_area"] != "(unknown)" else [],
                    "summary": item.get("summary") or "",
                },
                holdout_eval=False,
                notes=(cand.get("suggestion") or {}).get("why", ""),
                created_at=datetime.now(timezone.utc),
            )
            # Collision handling: append -2 / -3 / ...
            existing_ids = {s.id for s in product.snippets}
            if snippet.id in existing_ids:
                base, n = snippet.id, 2
                while f"{base}-{n}" in existing_ids:
                    n += 1
                snippet.id = f"{base}-{n}"
            try:
                product_dir = PRODUCTS_DIR / product_id
                save_snippet(product_dir, snippet)
                clear_cache()
                cand["decided"] = decision
            except Exception as e:
                _candidates_cache_path(product_id, nonce).write_text(
                    json.dumps(cache), encoding="utf-8",
                )
                return RedirectResponse(
                    url=f"/products/{product_id}/snippets/candidates?nonce={nonce}&idx={idx}&error={str(e)[:120]}",
                    status_code=303,
                )
        else:
            cand["decided"] = "skip"

        _candidates_cache_path(product_id, nonce).write_text(
            _json.dumps(cache), encoding="utf-8",
        )

    return RedirectResponse(
        url=f"/products/{product_id}/snippets/candidates?nonce={nonce}&idx={idx + 1}",
        status_code=303,
    )


# --- Snippet from run review (POST_V1_PLAN §4.4-C) --------------------------


@app.post("/products/{product_id}/runs/{run_id}/review/to-snippet")
async def review_item_to_snippet(product_id: str, run_id: str, request: Request):
    """Post a single item from the run review page as a new snippet.

    Feature-flagged: features.snippet_from_review_enabled must be on.
    Uses the item's title/body/url as the snippet body; polarity is
    supplied by the form.
    """
    from pipeline import features as _features
    if not _features.enabled("snippet_from_review_enabled", product_id):
        raise HTTPException(status_code=403, detail="snippet_from_review is disabled for this product")

    product = _product_or_404(product_id)
    form = await request.form()
    polarity = (form.get("polarity") or "").strip()
    if polarity not in (POSITIVE, NEGATIVE):
        raise HTTPException(status_code=400, detail=f"invalid polarity {polarity!r}")

    item_id = (form.get("item_id") or "").strip()
    if not item_id:
        raise HTTPException(status_code=400, detail="item_id is required")

    # Read the item from the run snapshot the review page uses.
    snap = _pick_review_snapshot(product_id, run_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshots for this run")
    items = _read_review_items(snap)
    item = next((i for i in items if i.get("id") == item_id), None)
    if item is None:
        raise HTTPException(status_code=404, detail=f"item {item_id!r} not in this run's review snapshot")

    snippet = Snippet(
        id=slugify((item.get("title") or item.get("body") or "review-snippet")[:60]),
        polarity=polarity,
        source_url=item.get("url") or None,
        title=item.get("title") or None,
        body=item.get("body") or "",
        labels={
            "is_topic_relevant": (polarity == POSITIVE),
            "areas": [item["primary_area"]] if item.get("primary_area") else [],
            "summary": item.get("summary") or "",
        },
        holdout_eval=False,
        notes=f"Added from run {run_id} review page.",
        created_at=datetime.now(timezone.utc),
    )
    existing_ids = {s.id for s in product.snippets}
    if snippet.id in existing_ids:
        base, n = snippet.id, 2
        while f"{base}-{n}" in existing_ids:
            n += 1
        snippet.id = f"{base}-{n}"

    save_snippet(PRODUCTS_DIR / product_id, snippet)
    clear_cache()
    return RedirectResponse(
        url=f"/products/{product_id}/runs/{run_id}/review?snippet_added={snippet.id}",
        status_code=303,
    )


# --- Runs + reports (UI 4) --------------------------------------------------
#
# Run trigger spawns `python -m pipeline.run --product <id>` as a subprocess.
# Status is read from data/<product>/run_logs/<run_id>.json (written by the
# pipeline at the end) and from a sidecar .running marker file we drop before
# starting the subprocess. Stdout/stderr go to .out so the user can see what
# happened on failure.


def _product_data_root(product_id: str) -> Path:
    return resolve_path(app_config()["paths"]["data_root"]) / product_id


def _run_logs_dir(product_id: str) -> Path:
    return _product_data_root(product_id) / "run_logs"


def _reports_root_for(product_id: str) -> Path:
    return resolve_path(app_config()["paths"]["reports_root"]) / product_id


def _project_python() -> str:
    """Pick the Python interpreter for spawned pipeline runs.

    Prefer the project's .venv (which has structlog, duckdb, praw, etc.)
    over `sys.executable` — the webui may be running under a different
    Python (e.g. system Anaconda) that doesn't have the pipeline deps.
    """
    root = Path(__file__).resolve().parent.parent
    candidates = [
        root / ".venv" / "Scripts" / "python.exe",   # Windows venv
        root / ".venv" / "bin" / "python",           # POSIX venv
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return sys.executable


def _out_looks_crashed(out_text: str) -> bool:
    """Heuristic: the subprocess wrote a Python traceback / fatal error."""
    if not out_text:
        return False
    tail = out_text[-1200:]
    return ("Traceback (most recent call last)" in tail
            or "ModuleNotFoundError" in tail
            or tail.rstrip().endswith("Error"))


def _list_runs(product_id: str) -> list[dict]:
    """Combine completed .json run logs + still-running .running markers
    + orphan crashed runs (have .out but no .json and no .running)."""
    logs_dir = _run_logs_dir(product_id)
    rows: dict[str, dict] = {}
    if not logs_dir.exists():
        return []

    for jf in logs_dir.glob("*.json"):
        try:
            payload = _json.loads(jf.read_text(encoding="utf-8"))
            rid = payload.get("run_id") or jf.stem
            rows[rid] = {
                "run_id": rid,
                "week_id": payload.get("week_id"),
                "status": payload.get("status") or "unknown",
                "stage_durations": payload.get("stage_durations") or {},
                "counters": payload.get("counters") or {},
                "running": False,
                "crashed": False,
            }
        except Exception:
            continue

    for mk in logs_dir.glob("*.running"):
        rid = mk.stem
        rows.setdefault(rid, {
            "run_id": rid, "week_id": None, "status": "running",
            "stage_durations": {}, "counters": {}, "running": True, "crashed": False,
        })

    # Orphan crashed: .out exists but neither .json nor .running.
    for of in logs_dir.glob("*.out"):
        rid = of.stem
        if rid in rows:
            continue
        try:
            tail = of.read_text(encoding="utf-8", errors="replace")
        except Exception:
            tail = ""
        if _out_looks_crashed(tail):
            rows[rid] = {
                "run_id": rid, "week_id": None, "status": "crashed",
                "stage_durations": {}, "counters": {}, "running": False, "crashed": True,
            }

    return sorted(rows.values(), key=lambda r: r["run_id"], reverse=True)


def _read_run(product_id: str, run_id: str) -> Optional[dict]:
    logs = _run_logs_dir(product_id)
    jf = logs / f"{run_id}.json"
    if jf.exists():
        try:
            return _json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def _run_is_running(product_id: str, run_id: str) -> bool:
    return (_run_logs_dir(product_id) / f"{run_id}.running").exists()


def _run_stdout(product_id: str, run_id: str) -> str:
    out = _run_logs_dir(product_id) / f"{run_id}.out"
    return out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""


def _report_dir_for_run(product_id: str, run_payload: Optional[dict]) -> Optional[Path]:
    if not run_payload or not run_payload.get("week_id"):
        return None
    candidate = _reports_root_for(product_id) / run_payload["week_id"]
    return candidate if (candidate / "index.html").exists() else None


@app.get("/products/{product_id}/runs", response_class=HTMLResponse)
def runs_index(request: Request, product_id: str,
                notice: Optional[str] = None, error: Optional[str] = None):
    product = _product_or_404(product_id)
    runs = _list_runs(product_id)
    # Use the plugin registry to resolve display names so the UI shows
    # "Media Coverage Sources" instead of raw plugin ids like "rss".
    try:
        from sources.registry import get_registry
        _reg = get_registry()
    except Exception:
        _reg = None

    def _type_display(t: str) -> str:
        if not t:
            return ""
        if _reg is not None:
            plugin = _reg.get(t)
            if plugin is not None:
                return plugin.manifest.display_name
        return t

    source_options = [
        {"id": s.get("id"),
         "type": s.get("type"),
         "type_display": _type_display(s.get("type") or ""),
         "n_streams": len((s.get("streams") or []))}
        for s in product.sources
    ]
    tr = product.time_range or {"mode": "incremental"}
    # POST_V1_PLAN §4.2 — pre-run readiness card.
    from webui.source_health import compute_readiness
    readiness = compute_readiness(product.sources)
    # Scheduler card — always renders (grays out when the flag is off so the
    # user sees why the card is inert).
    from pipeline import features as _features, scheduler as _scheduler
    schedule = _scheduler.load(product_id)
    schedule_enabled_flag = _features.enabled("scheduler_enabled", product_id)
    next_due = None
    if schedule.enabled:
        try:
            next_due = _scheduler.next_due_at(schedule).isoformat(timespec="minutes")
        except Exception:
            next_due = None
    return templates.TemplateResponse(
        "runs_list.html",
        {
            "request": request,
            "product": product,
            "runs": runs,
            "source_options": source_options,
            "time_range_summary": _summarize_time_range(tr),
            "source_readiness": readiness,
            "schedule": schedule,
            "schedule_cadences": _scheduler.CADENCES,
            "schedule_time_windows": _scheduler.TIME_WINDOWS,
            "schedule_flag_on": schedule_enabled_flag,
            "schedule_next_due": next_due,
            "notice": notice,
            "error": error,
        },
    )


# --- Schedule save (Runs and Reports tab) -----------------------------------


@app.post("/products/{product_id}/schedule")
async def schedule_save(product_id: str, request: Request):
    _product_or_404(product_id)
    from pipeline import scheduler as _scheduler
    form = await request.form()
    sched = _scheduler.Schedule(
        enabled=(form.get("enabled") in ("1", "on", "true")),
        cadence=(form.get("cadence") or "weekly").strip(),
        time_window=(form.get("time_window") or "last_week").strip(),
        hour=int(form.get("hour") or 9),
        minute=int(form.get("minute") or 0),
        # Preserve last_fired_at across saves — otherwise editing the form
        # would silently reset the cadence clock.
        last_fired_at=_scheduler.load(product_id).last_fired_at,
    )
    try:
        _scheduler.save(product_id, sched)
    except ValueError as e:
        return RedirectResponse(
            url=f"/products/{product_id}/runs?error={str(e)[:120]}",
            status_code=303,
        )
    return RedirectResponse(
        url=f"/products/{product_id}/runs?notice=schedule+saved",
        status_code=303,
    )


def _summarize_time_range(tr: dict) -> str:
    mode = tr.get("mode") or "incremental"
    if mode == "incremental":
        return "incremental (last cursor → now)"
    if mode == "last_week":
        return "last 7 days"
    if mode == "last_month":
        return "last 30 days"
    if mode == "range":
        return f"{tr.get('range_from') or '?'} → {tr.get('range_to') or '?'}"
    return mode


def _effective_stream_count(product, selected_source_ids: list[str]) -> tuple[int, str]:
    """Count how many streams would actually fetch given the current
    pause state + the user's source-id selection.

    Mirrors the fetch stage's pause precedence (pipeline/fetch.py):
      1. Global connection pause via connections.paused_types()
      2. Per-source `paused` flag on the product's source entry
      3. Per-stream `paused` flag on each stream

    Returns (count, error_message). error_message is a URL-safe hint
    explaining WHY nothing would fetch; only meaningful when count == 0.
    """
    from pipeline import connections
    globally_paused = connections.paused_types()

    allow: set[str] | None = (
        {s for s in selected_source_ids if s} if selected_source_ids else None
    )
    live = 0
    considered = 0
    for src in (product.sources or []):
        sid = src.get("id") or ""
        stype = src.get("type") or ""
        if allow is not None and sid not in allow:
            continue
        considered += 1
        if stype in globally_paused:
            continue
        if bool(src.get("paused")):
            continue
        for stream in (src.get("streams") or []):
            if not bool(stream.get("paused")):
                live += 1

    if live > 0:
        return live, ""
    if considered == 0:
        return 0, "no+sources+configured+for+this+product+—+add+one+on+the+Sources+page"
    if allow is None:
        return 0, "all+configured+sources+are+paused+—+un-pause+at+least+one+on+the+Sources+page"
    return 0, (
        "selected+source(s)+have+all+streams+paused+—+un-pause+on+the+Sources+"
        "page,+or+pick+a+source+that+isn%27t+fully+paused"
    )


@app.post("/products/{product_id}/runs")
async def runs_create(product_id: str, request: Request):
    product = _product_or_404(product_id)
    form = await request.form()
    skip_fetch = form.get("skip_fetch")
    skip_llm = form.get("skip_llm")
    # Multi-select of source ids; empty list = all sources (default).
    selected_sources = [v for v in form.getlist("source_ids") if v]
    # Optional per-run time-mode override; if not set, the persisted product
    # time_range is used by the orchestrator.
    time_mode_override = (form.get("time_mode_override") or "").strip()

    # Pre-flight: refuse to spawn a run when the effective selection has
    # zero un-paused streams. Without this, a user who picks a source
    # whose streams are ALL paused sees a "success" run with 0 items,
    # which is confusing. The pipeline would happily do that same
    # nothing — this just fails fast with a clear message.
    if not skip_fetch:
        n_live, msg = _effective_stream_count(product, selected_sources)
        if n_live == 0:
            return RedirectResponse(
                url=f"/products/{product_id}/runs?error={msg}",
                status_code=303,
            )

    # Pre-allocate a run_id so we can redirect immediately; the pipeline will
    # generate its own run_id internally too. We use ours only for the
    # .running marker so the listing shows the in-flight subprocess.
    import uuid
    from datetime import datetime, timezone
    marker_id = f"ui-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"

    logs_dir = _run_logs_dir(product_id)
    logs_dir.mkdir(parents=True, exist_ok=True)
    marker = logs_dir / f"{marker_id}.running"
    marker.write_text(f"started by webui at {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")

    out_path = logs_dir / f"{marker_id}.out"
    # Pass the marker id in as --run-id so the pipeline writes its terminal
    # .json under <marker_id>.json, matching our sidecars. Without this the
    # runs list shows two entries per run (marker + auto-generated) because
    # the .running cleanup path in run_detail looks for <marker_id>.json.
    cmd = [
        _project_python(), "-m", "pipeline.run",
        "--product", product_id,
        "--run-id", marker_id,
    ]
    if skip_fetch:
        cmd.append("--skip-fetch")
    if skip_llm:
        cmd.append("--skip-llm")
    if selected_sources:
        cmd.extend(["--source-ids", ",".join(selected_sources)])
    if time_mode_override and time_mode_override != "saved":
        cmd.extend(["--time-mode", time_mode_override])

    # Fire-and-forget: subprocess writes its real run log on completion.
    # We do NOT wait. The .running marker is cleaned up by a post-run check
    # the UI runs lazily when listing/reading status.
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parent.parent),
            stdout=open(out_path, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            # On Windows, DETACHED_PROCESS lets the child outlive a UI restart
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) if sys.platform == "win32" else 0,
        )
    except Exception as e:
        marker.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"failed to spawn pipeline: {e}")

    # Record the marker -> pid mapping so a future stop-button could read it.
    (logs_dir / f"{marker_id}.pid").write_text(str(proc.pid), encoding="utf-8")

    return RedirectResponse(url=f"/products/{product_id}/runs/{marker_id}", status_code=303)


# Stage order matches pipeline.run.main(). Kept in sync manually — small,
# rarely changes. Used by the flow-diagram parser below.
_PIPELINE_STAGES: tuple[str, ...] = (
    "fetch", "normalize", "filter",
    "relevance", "classify", "score", "group", "aggregate", "render",
)

_STAGE_LINE_RE = re.compile(r"stage=(\w+)")
_STAGE_SECONDS_RE = re.compile(r"seconds=([\d.]+)")


def _parse_stage_states(
    stdout: str,
    run_complete: bool,
    payload: Optional[dict] = None,
) -> list[dict]:
    """Scan the pipeline .out for stage_start / stage_done markers and produce
    a per-stage status list, in pipeline order.

    Statuses:
      pending  - not seen yet (only used while the run is still active)
      running  - stage_start seen, stage_done not yet
      done     - stage_done seen
      skipped  - run finished but stage never started (e.g. --skip-llm)
      failed   - fatal error before this stage's stage_done

    Duration is filled in for `done` stages from the `seconds=` field.

    Fallback: when `payload` is present (the run wrote its terminal JSON), we
    also merge in payload.stage_durations. This matters because a UI-triggered
    run's .out is written under the marker id, not the pipeline run-id — so
    viewing the completed run by its pipeline run-id gives us an empty stdout
    and stage_durations is the only source of truth we have.
    """
    states: dict[str, dict] = {
        s: {"stage": s, "status": "pending", "duration_s": None}
        for s in _PIPELINE_STAGES
    }
    llm_skipped_flag = False
    fatal_seen = False

    for line in (stdout or "").splitlines():
        if "stage_start" in line:
            m = _STAGE_LINE_RE.search(line)
            if m and m.group(1) in states:
                states[m.group(1)]["status"] = "running"
        elif "stage_done" in line:
            sm = _STAGE_LINE_RE.search(line)
            if sm and sm.group(1) in states:
                states[sm.group(1)]["status"] = "done"
                dm = _STAGE_SECONDS_RE.search(line)
                if dm:
                    try:
                        states[sm.group(1)]["duration_s"] = float(dm.group(1))
                    except ValueError:
                        pass
        elif "llm_skipped" in line:
            llm_skipped_flag = True
        elif "pipeline_failed" in line:
            fatal_seen = True

    # LLM-skipped explicitly turns the six LLM-gated stages into "skipped".
    if llm_skipped_flag:
        for s in ("relevance", "classify", "score", "group", "aggregate", "render"):
            if states[s]["status"] == "pending":
                states[s]["status"] = "skipped"

    if fatal_seen:
        for s in states.values():
            if s["status"] == "running":
                s["status"] = "failed"

    # Merge in payload.stage_durations: anything the payload knows ran must be
    # "done" even if the stdout parse missed it (e.g. .out written under a
    # different id, log rotated, etc.).
    if payload:
        for stage, secs in (payload.get("stage_durations") or {}).items():
            if stage in states and states[stage]["status"] in ("pending", "running"):
                states[stage]["status"] = "done"
                if states[stage]["duration_s"] is None:
                    try:
                        states[stage]["duration_s"] = float(secs)
                    except (TypeError, ValueError):
                        pass

    # Run has ended (payload written or crash detected). Anything still
    # "pending" means the stage never executed — usually --skip-fetch or the
    # run died so early the log has no stage_start entries.
    if run_complete:
        for s in states.values():
            if s["status"] == "pending":
                s["status"] = "skipped"
            elif s["status"] == "running":
                s["status"] = "failed"

    return [states[s] for s in _PIPELINE_STAGES]


@app.get("/products/{product_id}/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, product_id: str, run_id: str):
    product = _product_or_404(product_id)
    payload = _read_run(product_id, run_id)
    running = _run_is_running(product_id, run_id) and payload is None
    stdout = _run_stdout(product_id, run_id)
    report_dir = _report_dir_for_run(product_id, payload)

    crashed = False
    # Best-effort marker cleanup.
    marker = _run_logs_dir(product_id) / f"{run_id}.running"
    if payload is not None:
        # JSON exists => run completed normally.
        marker.unlink(missing_ok=True)
    elif running and _out_looks_crashed(stdout):
        # Subprocess wrote a Traceback and stopped — it's not coming back.
        # Clean up the marker so future visits show it as crashed, not stuck.
        marker.unlink(missing_ok=True)
        crashed = True
        running = False

    stage_states = _parse_stage_states(
        stdout, run_complete=(payload is not None or crashed), payload=payload,
    )
    captured = _captured_stages(product_id, run_id)
    source_flow = _per_source_counts(product_id, run_id, captured)
    # Fold per-stage totals into stage_states so the pill can show "stage N".
    for s in stage_states:
        s["total"] = source_flow["totals"].get(s["stage"])
    # POST_V1_PLAN §4.2 — post-run per-source health card. Only compute when
    # the run has finished (payload exists) since compute_health reads errors[].
    source_health_list = []
    if payload is not None:
        from webui.source_health import compute_health
        source_health_list = compute_health(payload, product.sources)

    # POST_V1_PLAN §4.11 — token usage card. Computed for any run with
    # llm_usage rows; gracefully handles empty table.
    #
    # We ALSO skip while a run is still in flight — the token card is only
    # meaningful after the run completes, and this route is polled every
    # 5s by the auto-refresh. Opening the warehouse on every poll invites
    # DuckDB lock contention with the subprocess (see storage.warehouse
    # retry loop). Skipping while `running` removes the contention entirely
    # for the common case.
    from pipeline import features as _features, token_usage as _tu
    token_totals: dict = {}
    if _features.enabled("token_monitor_enabled", product_id) and not running:
        token_totals = _tu.per_run_totals(product_id, run_id)
        # Add cost estimates per model
        if token_totals.get("total_tokens", 0) > 0:
            token_totals["estimated_cost_usd"] = _tu.estimate_cost_usd(
                model=(payload or {}).get("model", "") or "unknown",
                prompt_tokens=token_totals.get("prompt_tokens", 0),
                completion_tokens=token_totals.get("completion_tokens", 0),
                cached_input_tokens=token_totals.get("cached_input_tokens", 0),
            )
    # POST_V1_PLAN §4.10 — eval scorecard. Only rendered when evals_enabled
    # is on for this product AND a summary exists on disk.
    eval_summary = None
    if _features.enabled("evals_enabled", product_id):
        from pipeline import eval as _eval
        eval_summary = _eval.load_summary(product_id, run_id)

    return templates.TemplateResponse(
        "run_detail.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "payload": payload,
            "running": running,
            "crashed": crashed,
            "stdout_tail": stdout[-4000:] if stdout else "",
            "report_week": (payload or {}).get("week_id") if report_dir else None,
            "captured_stages": captured,
            "stage_states": stage_states,
            "source_flow": source_flow,
            "source_health": source_health_list,
            "token_totals": token_totals,
            "eval_summary": eval_summary,
        },
    )


# --- Trace viewer (POST_V1_PLAN §4.16) --------------------------------------
#
# Renders the JSONL span file at
# data/<pid>/temp_runs/<run_id>/trace.jsonl as a waterfall.
# Read-only. Trace file is written by pipeline/tracing.py during the run.


@app.get("/products/{product_id}/runs/{run_id}/trace", response_class=HTMLResponse)
def run_trace_view(request: Request, product_id: str, run_id: str):
    product = _product_or_404(product_id)
    trace_path = _temp_run_dir(product_id, run_id) / "trace.jsonl"
    spans: list[dict] = []
    if trace_path.exists():
        try:
            with trace_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        spans.append(_json.loads(line))
                    except Exception:
                        continue
        except Exception:
            spans = []

    # Compute a normalized waterfall — bar left offset + width as %
    if spans:
        # Sort by start_ts; earliest first
        spans.sort(key=lambda s: s.get("start_ts", 0))
        t_min = spans[0].get("start_ts", 0)
        t_max = max(s.get("end_ts", 0) for s in spans)
        total = max(t_max - t_min, 0.001)
        for s in spans:
            left = ((s.get("start_ts", t_min) - t_min) / total) * 100
            width = max(((s.get("end_ts", t_min) - s.get("start_ts", t_min)) / total) * 100, 0.5)
            s["_left_pct"] = round(left, 3)
            s["_width_pct"] = round(width, 3)

    return templates.TemplateResponse(
        "run_trace.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "spans": spans,
            "trace_path": str(trace_path),
        },
    )


# --- Per-stage snapshots (temp_runs) ----------------------------------------
#
# After each pipeline stage, pipeline.stage_capture writes a JSONL of the
# joined item view + a meta.json to data/<pid>/temp_runs/<run_id>/. These
# routes browse those files. Snapshots are kept forever; a delete button on
# the run detail page wipes just that run's temp dir.


def _temp_runs_root(product_id: str) -> Path:
    return _product_data_root(product_id) / "temp_runs"


def _temp_run_dir(product_id: str, run_id: str) -> Path:
    return _temp_runs_root(product_id) / run_id


def _per_source_counts(product_id: str, run_id: str, captured: list[dict]) -> dict:
    """Walk each captured stage's .jsonl and compute per-source "still in-flight"
    counts. Returns:

        {
          "sources": [(source_id, display_name), ...],   # union across stages
          "by_stage": {stage: {source_id: kept_count}},  # kept per source
          "totals":   {stage: total_kept},               # summed across sources
        }

    "Kept" for warehouse stages = filter_status in (None, 'passed') AND
    is_relevant in (None, True). For the fetch stage snapshot (which lists
    raw JSONL files, not warehouse rows), kept = sum(line_count) per source.
    """
    d = _temp_run_dir(product_id, run_id)
    by_stage: dict[str, dict[str, int]] = {}
    totals: dict[str, int] = {}
    displays: dict[str, str] = {}

    for meta in captured:
        stage = meta["stage"]
        jsonl = d / f"{stage}.jsonl"
        if not jsonl.exists():
            continue
        counts: dict[str, int] = {}
        total = 0
        try:
            with jsonl.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = _json.loads(line)
                    except Exception:
                        continue
                    src = row.get("source") or "unknown"
                    disp = row.get("source_display_name") or src
                    displays.setdefault(src, disp)
                    if stage == "fetch":
                        n = int(row.get("line_count") or 0)
                        counts[src] = counts.get(src, 0) + n
                        total += n
                    else:
                        fs = row.get("filter_status")
                        ir = row.get("is_relevant")
                        kept = fs in (None, "passed") and (ir is None or ir is True)
                        if kept:
                            counts[src] = counts.get(src, 0) + 1
                            total += 1
        except Exception:
            continue
        by_stage[stage] = counts
        totals[stage] = total

    # Stable source order: highest ever-seen count first, ties by name.
    max_by_source: dict[str, int] = {}
    for counts in by_stage.values():
        for src, n in counts.items():
            if n > max_by_source.get(src, 0):
                max_by_source[src] = n

    # Hide sources the user un-ticked on the run form. When --source-ids is
    # passed, both fetch (skips other sources) and filter (drops their
    # in-warehouse items as excluded_by_source) already scope the RUN's
    # data. But `_per_source_counts` reads captured snapshots, and the
    # fetch snapshot lists every raw file on disk for the week — including
    # files written by PRIOR runs. Without this filter, those excluded
    # sources still appeared as rows on the run detail page with counts >
    # 0 at normalize and 0 downstream, making it look like the source
    # filter didn't take effect. Read the definitive filter list from the
    # captured runtime.json instead of trying to reverse-engineer it from
    # snapshot counts.
    allowed = _run_source_id_filter(d)
    if allowed is not None:
        max_by_source = {s: n for s, n in max_by_source.items() if s in allowed}
    sources = sorted(max_by_source.keys(), key=lambda s: (-max_by_source[s], s))
    # Prefer the plugin manifest's display_name (e.g. "Media Coverage
    # Sources" for rss) over the per-item source_display_name — the latter
    # is per-stream and gives inconsistent labels when a product has
    # multiple streams under the same plugin.
    try:
        from sources.registry import get_registry
        _reg = get_registry()
    except Exception:
        _reg = None

    def _plugin_display(src_id: str) -> str:
        if _reg is not None:
            plugin = _reg.get(src_id)
            if plugin is not None:
                return plugin.manifest.display_name
        return displays.get(src_id, src_id)

    return {
        "sources": [(s, _plugin_display(s)) for s in sources],
        "by_stage": by_stage,
        "totals": totals,
    }


def _run_source_id_filter(temp_run_dir: Path) -> "set[str] | None":
    """Return the set of source ids the run was scoped to (from
    config_snapshot/runtime.json), or None if the run wasn't scoped
    (i.e. --source-ids wasn't passed and every configured source ran).

    Reads once per request; used by the run-detail per-source table to
    hide sources that were unticked on the runs form.
    """
    rt = temp_run_dir / "config_snapshot" / "runtime.json"
    if not rt.exists():
        return None
    try:
        blob = _json.loads(rt.read_text(encoding="utf-8"))
        raw = ((blob.get("runtime_context") or {}).get("cli_args") or {}).get("source_ids")
    except Exception:
        return None
    if not raw:
        return None
    ids = {s.strip() for s in str(raw).split(",") if s.strip()}
    return ids or None


def _captured_stages(product_id: str, run_id: str) -> list[dict]:
    """Return [{stage, row_count, duration_s, has_error}, ...] in capture order."""
    d = _temp_run_dir(product_id, run_id)
    idx_path = d / "stages.json"
    if not idx_path.exists():
        return []
    try:
        idx = _json.loads(idx_path.read_text(encoding="utf-8"))
        stages = idx.get("stages") or []
    except Exception:
        return []
    out: list[dict] = []
    for s in stages:
        meta_path = d / f"{s}.meta.json"
        row_count, duration, err = None, None, False
        if meta_path.exists():
            try:
                m = _json.loads(meta_path.read_text(encoding="utf-8"))
                row_count = m.get("row_count")
                duration = m.get("duration_s")
                err = bool(m.get("capture_error"))
            except Exception:
                pass
        out.append({"stage": s, "row_count": row_count, "duration_s": duration, "has_error": err})
    return out


_STAGE_VIEWS = ("in-flight", "dropped", "all")


def _row_is_in_flight(row: dict) -> bool:
    """Same "kept" definition used by the top-of-page source table:
    filter_status hasn't dropped it AND relevance didn't mark it not-relevant."""
    fs = row.get("filter_status")
    ir = row.get("is_relevant")
    return fs in (None, "passed") and (ir is None or ir is True)


def _read_stage_jsonl(
    path: Path, offset: int, limit: int, view: str = "all",
) -> tuple[list[dict], int, int]:
    """Read a slice of the JSONL, with an optional view filter (in-flight /
    dropped / all).

    Returns (rows_on_this_page, total_matching_view, total_in_file).
    Two counters so the sub-page can say "showing X of Y matching (Z total in warehouse)".

    The JSONL is per-run scale (hundreds of rows) so a full linear scan is fine.
    """
    if not path.exists():
        return [], 0, 0
    matches: list[dict] = []
    total_in_file = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            total_in_file += 1
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except Exception:
                row = {"_parse_error": True, "_raw": line[:200]}
            if view == "in-flight" and not _row_is_in_flight(row):
                continue
            if view == "dropped" and _row_is_in_flight(row):
                continue
            matches.append(row)
    total_matching = len(matches)
    return matches[offset:offset + limit], total_matching, total_in_file


@app.get("/products/{product_id}/runs/{run_id}/stages/{stage}", response_class=HTMLResponse)
def stage_snapshot(
    request: Request,
    product_id: str,
    run_id: str,
    stage: str,
    offset: int = 0,
    limit: int = 50,
    view: str = "in-flight",
):
    product = _product_or_404(product_id)
    d = _temp_run_dir(product_id, run_id)
    jsonl_path = d / f"{stage}.jsonl"
    meta_path = d / f"{stage}.meta.json"
    if not jsonl_path.exists():
        raise HTTPException(status_code=404, detail=f"no snapshot for stage {stage!r}")

    # Fetch stage has no filter_status concept — force 'all'.
    if stage == "fetch" or view not in _STAGE_VIEWS:
        view = "all"

    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows, total, total_in_file = _read_stage_jsonl(jsonl_path, offset, limit, view=view)

    meta: dict = {}
    if meta_path.exists():
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    # Union of keys seen across the current page — stable-ish column order:
    # base identity first, then classification, then everything else alphabetical.
    preferred = [
        "id", "source", "week_id", "created_at", "author", "title", "body",
        "filter_status", "is_relevant", "relevance_score",
        "primary_area", "sentiment", "content_types_json", "summary",
        "severity", "score",
    ]
    keys_seen = {k for r in rows for k in r.keys()}
    ordered = [k for k in preferred if k in keys_seen] + sorted(
        k for k in keys_seen if k not in preferred
    )

    # Kept-count so the sub-page header can show both "in warehouse" (rows in
    # this JSONL) and "in-flight" (items where filter_status is passed/null
    # and is_relevant isn't False) — matches the parent run detail table.
    kept_total = None
    try:
        captured_for_kept = _captured_stages(product_id, run_id)
        source_flow_kept = _per_source_counts(product_id, run_id, captured_for_kept)
        kept_total = source_flow_kept.get("totals", {}).get(stage)
    except Exception:
        pass

    return templates.TemplateResponse(
        "stage_snapshot.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "stage": stage,
            "meta": meta,
            "rows": rows,
            "columns": ordered,
            "total": total,
            "total_in_file": total_in_file,
            "offset": offset,
            "limit": limit,
            "view": view,
            "views_available": _STAGE_VIEWS if stage != "fetch" else ("all",),
            "kept_total": kept_total,
            "all_stages": _captured_stages(product_id, run_id),
        },
    )


@app.get("/products/{product_id}/runs/{run_id}/stages/{stage}/download")
def stage_snapshot_download(product_id: str, run_id: str, stage: str):
    _product_or_404(product_id)
    p = _temp_run_dir(product_id, run_id) / f"{stage}.jsonl"
    if not p.exists():
        raise HTTPException(status_code=404, detail="no snapshot")
    return FileResponse(
        str(p),
        media_type="application/x-ndjson",
        filename=f"{run_id}-{stage}.jsonl",
    )


@app.post("/products/{product_id}/runs/{run_id}/stages/delete")
def stage_snapshots_delete(product_id: str, run_id: str):
    _product_or_404(product_id)
    d = _temp_run_dir(product_id, run_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    return RedirectResponse(url=f"/products/{product_id}/runs/{run_id}", status_code=303)


# --- Per-run config snapshot browser ----------------------------------------
#
# stage_capture.snapshot_config copies the config that produced each run into
# temp_runs/<run_id>/config_snapshot/. These routes browse it so you can answer
# "what settings did this run use?" long after the live config has changed.


def _config_snapshot_dir(product_id: str, run_id: str) -> Path:
    return _temp_run_dir(product_id, run_id) / "config_snapshot"


def _list_config_snapshot_files(product_id: str, run_id: str) -> list[dict]:
    """Return a flat list of files in the snapshot, sorted for stable display.
    Each entry: {relpath, name, group, size_bytes}."""
    root = _config_snapshot_dir(product_id, run_id)
    if not root.exists():
        return []
    entries: list[dict] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        group = "global" if "/" not in rel else rel.split("/", 1)[0]
        entries.append({
            "relpath": rel,
            "name": p.name,
            "group": group,
            "size_bytes": p.stat().st_size,
        })
    return entries


def _safe_snapshot_file(product_id: str, run_id: str, relpath: str) -> Path:
    """Resolve `relpath` inside the snapshot dir, rejecting anything that
    escapes it (path traversal defense)."""
    root = _config_snapshot_dir(product_id, run_id).resolve()
    candidate = (root / relpath).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid path")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail=f"file {relpath!r} not in snapshot")
    return candidate


@app.get("/products/{product_id}/runs/{run_id}/config", response_class=HTMLResponse)
def run_config_snapshot(request: Request, product_id: str, run_id: str):
    product = _product_or_404(product_id)
    entries = _list_config_snapshot_files(product_id, run_id)
    runtime = None
    runtime_path = _config_snapshot_dir(product_id, run_id) / "runtime.json"
    if runtime_path.exists():
        try:
            runtime = _json.loads(runtime_path.read_text(encoding="utf-8"))
        except Exception:
            runtime = None
    return templates.TemplateResponse(
        "run_config.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "entries": entries,
            "runtime": runtime,
            "has_snapshot": bool(entries),
        },
    )


@app.get("/products/{product_id}/runs/{run_id}/config/view", response_class=HTMLResponse)
def run_config_snapshot_view(request: Request, product_id: str, run_id: str, relpath: str):
    product = _product_or_404(product_id)
    path = _safe_snapshot_file(product_id, run_id, relpath)
    body = path.read_text(encoding="utf-8", errors="replace")
    return templates.TemplateResponse(
        "run_config_file.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "relpath": relpath,
            "name": path.name,
            "body": body,
            "size_bytes": path.stat().st_size,
        },
    )


@app.get("/products/{product_id}/runs/{run_id}/config/download")
def run_config_snapshot_download(product_id: str, run_id: str, relpath: str):
    _product_or_404(product_id)
    path = _safe_snapshot_file(product_id, run_id, relpath)
    return FileResponse(
        str(path),
        media_type="text/yaml" if path.suffix in (".yaml", ".yml") else "application/octet-stream",
        filename=f"{run_id}-{path.name}",
    )


# --- Per-run post review ----------------------------------------------------
#
# Every item in the run + the reason it was kept or dropped. Reads the latest
# stage snapshot (classify.jsonl if it exists; else the last one written) so
# we see the item's final filter_status / is_relevant after every stage that
# has run so far.

_FILTER_STATUS_HELP = {
    "passed":                  "Kept by filter — text/engagement/dedup all OK.",
    "dropped:too_short":       "Body under min_body_chars AND title < 20 chars AND no KB/CVE watchlist match.",
    "dropped:duplicate_url":   "Canonical URL was already seen earlier in the batch.",
    "dropped:duplicate_title": "Title matches an earlier item's simhash within grouping.simhash_hamming_threshold.",
    "dropped:low_engagement":  "Upvotes AND comments both below fetching.default_engagement_threshold (raise to 0 to keep everything).",
    "dropped:deleted_or_empty":"Body was [deleted] / [removed] / empty AND no title.",
    "dropped:not_topic_relevant": "Relevance LLM decided this isn't on-topic (score above filter.relevance_drop_confidence).",
    "dropped:not_relevant":       "Relevance LLM decided this isn't relevant (older path).",
    "classification_failed":   "Classify stage failed for this item (LLM error or schema violation).",
    "dropped:conditional_violation": "Classify stage's structured output violated a conditional schema constraint.",
}


def _outcome_class(filter_status: str | None, is_relevant) -> str:
    """CSS class hint for the row: 'kept' / 'dropped-filter' / 'dropped-relevance' / 'dropped-classify' / 'inflight'."""
    if not filter_status:
        return "inflight"
    if filter_status == "passed":
        if is_relevant is True:
            return "kept"
        if is_relevant is False:
            return "dropped-relevance"
        return "inflight"
    if "not_relevant" in filter_status or "not_topic_relevant" in filter_status:
        return "dropped-relevance"
    if "classification" in filter_status:
        return "dropped-classify"
    return "dropped-filter"


def _outcome_label(filter_status: str | None, is_relevant) -> str:
    if not filter_status:
        return "not filtered yet"
    if filter_status == "passed":
        if is_relevant is True:
            return "KEPT (relevant)"
        if is_relevant is False:
            return "dropped by relevance"
        return "kept by filter (pending relevance)"
    if filter_status.startswith("dropped:"):
        return f"dropped: {filter_status.split(':', 1)[1]}"
    return filter_status


def _pick_review_snapshot(product_id: str, run_id: str) -> Optional[Path]:
    """Pick the most complete snapshot for the review list.

    Order of preference (each is a *superset* of the last in terms of state
    populated per item):
      classify > relevance > filter > normalize
    Then fall back to whichever stage was last captured.
    """
    d = _temp_run_dir(product_id, run_id)
    for stage in ("classify", "relevance", "filter", "normalize"):
        p = d / f"{stage}.jsonl"
        if p.exists():
            return p
    # last-captured fallback
    idx = d / "stages.json"
    if idx.exists():
        try:
            stages = _json.loads(idx.read_text(encoding="utf-8")).get("stages") or []
            for s in reversed(stages):
                p = d / f"{s}.jsonl"
                if p.exists() and s != "fetch":
                    return p
        except Exception:
            pass
    return None


def _read_review_items(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = _json.loads(line)
            except Exception:
                continue
            fs = row.get("filter_status")
            ir = row.get("is_relevant")
            row["_outcome_class"] = _outcome_class(fs, ir)
            row["_outcome_label"] = _outcome_label(fs, ir)
            row["_reason_help"] = _FILTER_STATUS_HELP.get(fs or "", "")
            rows.append(row)
    return rows


@app.get("/products/{product_id}/runs/{run_id}/review", response_class=HTMLResponse)
def run_review(
    request: Request,
    product_id: str,
    run_id: str,
    outcome: Optional[str] = None,
    source: Optional[str] = None,
    reason: Optional[str] = None,
    q: Optional[str] = None,
):
    product = _product_or_404(product_id)
    snap = _pick_review_snapshot(product_id, run_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshots for this run")

    items = _read_review_items(snap)

    # Facets before filtering, so dropdowns show the full menu.
    reason_counts: dict[str, int] = {}
    for i in items:
        r = i.get("filter_status") or "(none — pre-filter)"
        reason_counts[r] = reason_counts.get(r, 0) + 1
    facets = {
        "outcomes": sorted({i["_outcome_class"] for i in items}),
        "sources":  sorted({i.get("source") for i in items if i.get("source")}),
        # (label, value, count) — sorted by count desc so common reasons come first
        "reasons":  sorted(
            [(r, r, n) for r, n in reason_counts.items()],
            key=lambda t: (-t[2], t[0]),
        ),
    }
    outcome_counts = {}
    for i in items:
        outcome_counts[i["_outcome_class"]] = outcome_counts.get(i["_outcome_class"], 0) + 1

    # Apply filters.
    filtered = items
    if outcome:
        filtered = [i for i in filtered if i["_outcome_class"] == outcome]
    if source:
        filtered = [i for i in filtered if i.get("source") == source]
    if reason:
        if reason == "(none — pre-filter)":
            filtered = [i for i in filtered if not i.get("filter_status")]
        else:
            filtered = [i for i in filtered if i.get("filter_status") == reason]
    if q:
        needle = q.lower()
        filtered = [i for i in filtered
                    if needle in (i.get("title") or "").lower()
                    or needle in (i.get("body") or "").lower()
                    or needle in (i.get("author") or "").lower()]

    from pipeline import features as _features_ref
    return templates.TemplateResponse(
        "run_review.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "snapshot_stage": snap.stem,
            "items": filtered,
            "total_all": len(items),
            "total_shown": len(filtered),
            "facets": facets,
            "outcome_counts": outcome_counts,
            "filters": {"outcome": outcome or "", "source": source or "", "reason": reason or "", "q": q or ""},
            "snippet_from_review_enabled": _features_ref.enabled(
                "snippet_from_review_enabled", product_id,
            ),
        },
    )


@app.get("/products/{product_id}/runs/{run_id}/review/detail", response_class=HTMLResponse)
def run_review_detail(request: Request, product_id: str, run_id: str, item_id: str):
    product = _product_or_404(product_id)
    snap = _pick_review_snapshot(product_id, run_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="no snapshots for this run")

    item = None
    for row in _read_review_items(snap):
        if row.get("id") == item_id:
            item = row
            break
    if item is None:
        raise HTTPException(status_code=404, detail=f"item {item_id!r} not in this run's snapshot")

    # Walk every stage snapshot to build a per-stage state transition list.
    journey: list[dict] = []
    d = _temp_run_dir(product_id, run_id)
    idx_path = d / "stages.json"
    stages: list[str] = []
    if idx_path.exists():
        try:
            stages = _json.loads(idx_path.read_text(encoding="utf-8")).get("stages") or []
        except Exception:
            pass
    prev_fs, prev_ir = "<absent>", "<absent>"
    for stage in stages:
        p = d / f"{stage}.jsonl"
        if not p.exists() or stage == "fetch":
            journey.append({"stage": stage, "state": None, "changed": False, "note": "fetch snapshot is per-file" if stage == "fetch" else "no snapshot"})
            continue
        row = None
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = _json.loads(line)
                except Exception:
                    continue
                if r.get("id") == item_id:
                    row = r
                    break
        if row is None:
            journey.append({"stage": stage, "state": None, "changed": False, "note": "not present"})
            continue
        fs = row.get("filter_status")
        ir = row.get("is_relevant")
        changed = (fs != prev_fs) or (ir != prev_ir)
        journey.append({
            "stage": stage,
            "state": {"filter_status": fs, "is_relevant": ir, "relevance_score": row.get("relevance_score")},
            "changed": changed,
        })
        prev_fs, prev_ir = fs, ir

    return templates.TemplateResponse(
        "run_review_detail.html",
        {
            "request": request,
            "product": product,
            "run_id": run_id,
            "item": item,
            "journey": journey,
            "reason_help": _FILTER_STATUS_HELP.get(item.get("filter_status") or "", ""),
        },
    )


# --- Fetched-items browser (debugging) --------------------------------------
#
# Surfaces the per-product DuckDB `items` table so the user can see what
# fetch + normalize produced, plus the optional classification rows. Linked
# from the Runs page. Read-only.


@app.get("/products/{product_id}/items", response_class=HTMLResponse)
def items_list(
    request: Request,
    product_id: str,
    source: Optional[str] = None,
    week: Optional[str] = None,
    relevance: Optional[str] = None,   # 'yes' | 'no' | 'unset'
    offset: int = 0,
):
    from pipeline import storage
    product = _product_or_404(product_id)
    set_current_product(product)
    limit = 50

    ctx_empty = {
        "request": request, "product": product,
        "items": [], "total": 0,
        "filters": {"source": source or "", "week": week or "",
                    "relevance": relevance or "", "offset": 0, "limit": limit},
        "facets": {"sources": [], "weeks": []},
        "no_warehouse": True,
    }
    if not storage.warehouse_path().exists():
        return templates.TemplateResponse("items_list.html", ctx_empty)

    where: list[str] = []
    params: list = []
    if source:
        where.append("source = ?"); params.append(source)
    if week:
        where.append("week_id = ?"); params.append(week)
    if relevance == "yes":
        where.append("is_relevant = TRUE")
    elif relevance == "no":
        where.append("is_relevant = FALSE")
    elif relevance == "unset":
        where.append("is_relevant IS NULL")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    try:
        total = storage.query(f"SELECT COUNT(*) AS n FROM items{where_sql}", params)[0]["n"]
        rows = storage.query(
            "SELECT id, source, source_display_name, week_id, created_at, author, "
            "url, title, body, is_relevant, filter_status, is_reply, author_intent "
            f"FROM items{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        sources = [r["source"] for r in storage.query(
            "SELECT DISTINCT source FROM items ORDER BY source")]
        weeks = [r["week_id"] for r in storage.query(
            "SELECT DISTINCT week_id FROM items ORDER BY week_id DESC")]
    except Exception:
        # Empty / fresh schema, no rows yet.
        ctx_empty["no_warehouse"] = False
        return templates.TemplateResponse("items_list.html", ctx_empty)

    return templates.TemplateResponse("items_list.html", {
        "request": request, "product": product,
        "items": rows, "total": total,
        "filters": {"source": source or "", "week": week or "",
                    "relevance": relevance or "", "offset": offset, "limit": limit},
        "facets": {"sources": sources, "weeks": weeks},
        "no_warehouse": False,
    })


@app.get("/products/{product_id}/items/detail", response_class=HTMLResponse)
def item_detail(request: Request, product_id: str, item_id: str):
    from pipeline import storage
    product = _product_or_404(product_id)
    set_current_product(product)

    rows = storage.query("SELECT * FROM items WHERE id = ?", [item_id])
    if not rows:
        raise HTTPException(status_code=404, detail=f"item {item_id} not found")
    item = rows[0]

    cls_rows = storage.query(
        "SELECT * FROM item_classifications WHERE item_id = ?", [item_id])
    classification = cls_rows[0] if cls_rows else None

    areas = storage.query(
        "SELECT area, is_primary FROM item_areas WHERE item_id = ? ORDER BY is_primary DESC, area",
        [item_id])

    # Try to recover the original fetched record from the raw JSONL it came from.
    raw_json = None
    raw_ref = item.get("raw_ref")
    if raw_ref:
        raw_path = Path(raw_ref)
        if raw_path.exists():
            try:
                import json as _json
                with raw_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        rec = _json.loads(line)
                        if (rec.get("external_id") == item["external_id"]
                                and rec.get("source") == item["source"]):
                            raw_json = rec
                            break
            except Exception:
                pass

    return templates.TemplateResponse("item_detail.html", {
        "request": request, "product": product, "item": item,
        "classification": classification, "areas": areas, "raw_json": raw_json,
    })


@app.get("/products/{product_id}/reports/{week_id}/")
def report_index(product_id: str, week_id: str):
    return _serve_report(product_id, week_id, "index.html")


@app.get("/products/{product_id}/reports/{week_id}/{filename}")
def report_file(product_id: str, week_id: str, filename: str):
    if "/" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    return _serve_report(product_id, week_id, filename)


# Nested subresources (chart PNGs under data/, etc.) — matches any path
# with slashes so `<img src="data/trend_bugs.png">` resolves under
# reports/<product>/<week>/. Path traversal is defended via the resolve()
# containment check inside _serve_report_subpath.
@app.get("/products/{product_id}/reports/{week_id}/{subpath:path}")
def report_subpath(product_id: str, week_id: str, subpath: str):
    return _serve_report_subpath(product_id, week_id, subpath)


def _serve_report(product_id: str, week_id: str, filename: str):
    path = _reports_root_for(product_id) / week_id / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"no report file at {path}")
    return FileResponse(str(path))


def _serve_report_subpath(product_id: str, week_id: str, subpath: str):
    # Refuse anything that could escape the report dir.
    if ".." in subpath.replace("\\", "/").split("/"):
        raise HTTPException(status_code=400, detail="invalid path")
    week_root = (_reports_root_for(product_id) / week_id).resolve()
    target = (week_root / subpath).resolve()
    try:
        target.relative_to(week_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes report dir")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"no report file at {target}")
    return FileResponse(str(target))


# --- API: refresh caches (used by editors that mutate config) ---------------


@app.post("/api/refresh")
def refresh_caches() -> dict:
    clear_cache()
    return {"ok": True}


# --- Entry point ------------------------------------------------------------


def serve(host: str = "127.0.0.1", port: int = 8766, reload: bool = False) -> None:
    uvicorn.run(
        "webui.app:app" if reload else app,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


def main() -> None:
    # Cross-platform preflight (see pipeline/preflight.py).
    from pipeline import preflight
    preflight.check()

    import argparse

    ap = argparse.ArgumentParser(description="ProductMonitor — local admin UI")
    ap.add_argument("--host", default="127.0.0.1", help="Bind address (local-only default)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--reload", action="store_true", help="Auto-reload on file change (dev)")
    args = ap.parse_args()
    serve(args.host, args.port, args.reload)


if __name__ == "__main__":
    main()
