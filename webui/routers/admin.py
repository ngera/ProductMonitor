"""Admin pages: tuning, prompts, tokens, features, system, media catalog.

Split out of webui/app.py (ADR-0026). Shared helpers live in
webui/core.py and are reached as `core.<name>` so a test patch there
reaches every caller.
"""

from __future__ import annotations

import json as _json
import shutil
import yaml
from datetime import datetime, timezone
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pathlib import Path
from pipeline.product import available_products, load_product
from typing import Any, Optional

from webui import core

router = APIRouter()


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
     "How long a post’s body must be to keep it. Shorter posts are dropped as "
     "noise (unless the title is long enough, or it mentions a CVE). "
     "Lower this if you get little feedback and want to keep short posts. "
     "Raise it if one-line comments are cluttering results.",
     "Filter"),
    ("fetching", "default_engagement_threshold", "int", 5,
     "Minimum likes/upvotes/comments before a post is kept. "
     "0 keeps everything (good for quiet support forums). "
     "5 works well for Reddit-scale traffic. "
     "Higher values keep only posts that already got community attention.",
     "Filter"),
    ("filter", "relevance_drop_confidence", "float", 0.7,
     "How sure the “is this about my product?” check must be before it throws "
     "a post away. Higher keeps more borderline posts (costs more LLM usage). "
     "Lower drops more early (cheaper, but may discard useful posts).",
     "Filter"),
    ("grouping", "simhash_hamming_threshold", "int", 4,
     "How similar two titles must be to treat them as the same complaint. "
     "0 = only exact matches. "
     "4 (default) catches most rephrasings of the same issue. "
     "Higher merges more aggressively — useful when many people file the same bug differently.",
     "Filter"),
    # --- Fetching (per-source caps) ---
    ("fetching", "new_limit", "int", 1000,
     "Max items pulled from each source’s “new” feed per run. "
     "Higher = more coverage, slower runs, more classify cost. "
     "Lower = faster runs; you may miss posts between runs on busy sources.",
     "Fetching"),
    ("fetching", "top_limit", "int", 100,
     "Max items from each source’s “top” / best-ranked feed. "
     "Usually kept lower than “new” because these posts are already high-signal.",
     "Fetching"),
    ("fetching", "controversial_limit", "int", 50,
     "Max items from the “controversial” feed (polarizing posts). "
     "Often surfaces love/hate reactions to bugs and design choices. "
     "Set to 0 to skip this feed entirely.",
     "Fetching"),
    ("fetching", "max_comments_per_post", "int", 500,
     "Max comments fetched under a single post. "
     "Stops runaway fetches on viral threads. "
     "500 is usually enough; lower for faster runs, higher for deep community analysis.",
     "Fetching"),
    ("fetching", "parent_context_body_chars", "int", 500,
     "How much of the parent post is pasted into each comment when classifying, "
     "so the model can understand short replies. "
     "More text = better context but higher token cost.",
     "Fetching"),
    ("fetching", "sleep_between_streams_seconds", "int", 2,
     "Pause between hitting different feeds on the same source. "
     "Keeps public APIs from rate-limiting you. Raise this if you see 429 errors.",
     "Fetching"),
    ("fetching", "triangulate", "bool", True,
     "When on: fetch new + top + controversial and merge them (duplicates removed). "
     "When off: only “new” — faster, but you may miss older posts that resurfaced in rankings.",
     "Fetching"),
    ("fetching", "fetch_all_comments", "bool", True,
     "When on: keep every reply under a kept post (even quiet ones). "
     "When off: keep only high-engagement comments — fewer items, but you may miss "
     "useful bug repro steps or workarounds.",
     "Fetching"),
    # --- Grouping / Scoring / Reporting ---
    ("grouping", "feature_implicated_min_confidence", "float", 0.5,
     "How confident the model must be before blaming a feature/product for an issue. "
     "Higher = fewer false “X caused this” labels. "
     "Lower = more aggressive attribution (noisier, catches more real culprits).",
     "Grouping"),
    ("scoring", "recency_halflife_days", "int", 7,
     "How fast older posts lose importance in rankings. "
     "At this many days old, a post scores half as much as a brand-new one. "
     "Lower favors this week’s chatter; higher keeps month-old issues visible longer.",
     "Scoring"),
    ("reporting", "trend_weeks", "int", 4,
     "Legacy report only: how many past weeks appear in the trend section. "
     "Digest v2 ignores this and uses its own trend settings.",
     "Reporting"),
    ("reporting", "top_items_per_area", "int", 10,
     "Legacy report only: max posts shown per topic area. "
     "Digest v2 uses its own per-section limits instead.",
     "Reporting"),
    ("reporting", "top_groups_per_area", "int", 10,
     "Legacy report only: max issue groups shown per topic area. "
     "Digest v2 uses persistent issues instead of this setting.",
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


# /healthz moved to webui.routers.health (ADR-0026). Its former private
# helpers (_run_id_started_at, _last_successful_run_age_seconds) now
# live in webui.services.runs so other routes that need them can share
# the implementation.


@router.get("/admin/tuning", response_class=HTMLResponse)
def admin_tuning(request: Request, saved: int = 0, error: Optional[str] = None):
    return core.templates.TemplateResponse(
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


@router.post("/admin/tuning")
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
    core.clear_cache()

    return RedirectResponse(url="/admin/tuning?saved=1", status_code=303)


# --- Admin: feature flags (POST_V1_PLAN §4.13, ADR-0006) --------------------
#
# UI to flip feature flags in config/features.yaml without opening the file.
# Product-level overrides in products/<pid>/features.yaml still work but
# aren't editable here (edit per-product features.yaml directly for those).


# Friendly section headings for Admin → Features (general operator audience).
_PHASE_ORDER = {
    "trust_plugins_dir": "Sources & plugins",
    "scrapecreators_enabled": "Sources & plugins",
    "assistant_llm_enabled": "AI helper",
    "token_monitor_enabled": "AI helper",
    "prompt_caching_enabled": "AI helper",
    "prompt_suggestions_enabled": "AI helper",
    "rationale_enabled": "AI helper",
    "evals_enabled": "Training & quality",
    "snippet_candidates_enabled": "Training & quality",
    "snippet_from_review_enabled": "Training & quality",
    "wizard_enabled": "Product setup",
    "wizard_v2_enabled": "Product setup",
    "digest_v2_enabled": "Reports",
    "competition_analysis_enabled": "Reports",
    "scheduler_enabled": "Runs & notifications",
    "http_retry_enabled": "Runs & notifications",
    "fetch_concurrency_enabled": "Runs & notifications",
    "run_notifications_enabled": "Runs & notifications",
    "observability_traces_enabled": "Admin tools",
    "admin_token_tracker_enabled": "Admin tools",
}

# Display name + plain-language help for a general product-owner audience.
_FEATURE_UI: dict[str, dict[str, str]] = {
    "trust_plugins_dir": {
        "display": "Allow custom add-on sources",
        "help": (
            "Lets ProductMonitor load extra source connectors someone dropped "
            "into this install. Leave off unless you know you installed trusted add-ons."
        ),
    },
    "assistant_llm_enabled": {
        "display": "AI setup assistant",
        "help": (
            "Uses an AI model to help draft product profiles, suggest sources, "
            "and propose taxonomy while you set up a product. Requires an "
            "Assistant LLM under Connections."
        ),
    },
    "token_monitor_enabled": {
        "display": "Track AI usage",
        "help": (
            "Keeps a log of how much AI usage each run consumes so you can "
            "review cost and activity later."
        ),
    },
    "prompt_caching_enabled": {
        "display": "Speed up repeated AI calls",
        "help": (
            "When your AI provider supports it, reuses stable parts of prompts "
            "so common calls are cheaper and a bit faster. Safe to leave on."
        ),
    },
    "scrapecreators_enabled": {
        "display": "Social sources via ScrapeCreators",
        "help": (
            "Turns on Reddit, X, and TikTok fetching through ScrapeCreators. "
            "You’ll need a ScrapeCreators API key on Connections."
        ),
    },
    "evals_enabled": {
        "display": "Quality checks after each run",
        "help": (
            "Compares the AI’s classifications against examples you’ve labeled, "
            "so you can spot when results get worse over time."
        ),
    },
    "snippet_candidates_enabled": {
        "display": "Suggest training examples",
        "help": (
            "Asks the AI to propose new example posts you can save as training "
            "data to improve future results."
        ),
    },
    "snippet_from_review_enabled": {
        "display": "Save examples while reviewing",
        "help": (
            "While looking at individual posts, lets you quickly mark one as a "
            "training example for later runs."
        ),
    },
    "wizard_enabled": {
        "display": "Classic product setup wizard",
        "help": (
            "Shows the older step-by-step product setup flow. Most people can "
            "leave this off and use Guided product setup instead."
        ),
    },
    "wizard_v2_enabled": {
        "display": "Guided product setup",
        "help": (
            "The recommended way to add a product: describe it, confirm the "
            "draft profile, pick sources, calibrate on real posts, then finish."
        ),
    },
    "prompt_suggestions_enabled": {
        "display": "Suggest prompt improvements",
        "help": (
            "On a product’s prompts screen, the AI can suggest wording changes "
            "based on recent feedback you’ve collected."
        ),
    },
    "rationale_enabled": {
        "display": "“Why this matters” explanations",
        "help": (
            "Lets you generate short plain-language explanations for items in "
            "a report when you ask for them."
        ),
    },
    "observability_traces_enabled": {
        "display": "Detailed run diagnostics",
        "help": (
            "Saves extra detail about each run to help troubleshoot failures "
            "or slow stages. Slightly more storage use."
        ),
    },
    "digest_v2_enabled": {
        "display": "Modern weekly digest report",
        "help": (
            "Builds the current digest-style HTML report (headlines, ongoing "
            "issues, trends). Recommended for most products."
        ),
    },
    "scheduler_enabled": {
        "display": "Automatic scheduled runs",
        "help": (
            "Starts monitoring runs on each product’s schedule (for example "
            "weekly) without you clicking Run each time."
        ),
    },
    "admin_token_tracker_enabled": {
        "display": "AI usage overview page",
        "help": (
            "Shows Admin → Tokens, a summary of AI usage and estimated cost "
            "across your products."
        ),
    },
    "http_retry_enabled": {
        "display": "Retry temporary fetch errors",
        "help": (
            "If a source briefly fails (busy server, rate limit, timeout), "
            "try again automatically before giving up. Best left on."
        ),
    },
    "run_notifications_enabled": {
        "display": "Notify when a run finishes",
        "help": (
            "Sends a short status message to a webhook URL you configure "
            "(for Slack, Teams, or similar). Nothing is sent until that URL is set."
        ),
    },
    "fetch_concurrency_enabled": {
        "display": "Fetch sources in parallel",
        "help": (
            "Pulls from multiple sources at once so runs finish faster. "
            "If a site starts blocking you, slow things down under Admin → Tuning."
        ),
    },
    "competition_analysis_enabled": {
        "display": "Competitor tracking in reports",
        "help": (
            "Adds competitor fields during setup and a Competition section in "
            "the digest. Currently off while this capability is being improved."
        ),
    },
}


@router.get("/admin/prompts", response_class=HTMLResponse)
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
    return core.templates.TemplateResponse(
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


@router.post("/admin/prompts")
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


@router.get("/admin/prompts/export")
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


@router.post("/admin/prompts/import")
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


@router.get("/admin", response_class=HTMLResponse)
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


@router.get("/admin/tokens", response_class=HTMLResponse)
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

    return core.templates.TemplateResponse(
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


@router.get("/admin/tokens.csv")
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


def _admin_features_install_health(assistant_summary: dict) -> dict:
    """Best-effort Install health rows for the admin features right rail."""
    rows: list[dict] = []

    asst_ok = bool(
        assistant_summary.get("configured") and assistant_summary.get("enabled")
    )
    rows.append({
        "label": "Assistant LLM",
        "value": "reachable" if asst_ok else (
            "flag off" if assistant_summary.get("configured") else "not configured"
        ),
        "ok": asst_ok,
    })

    # Sum DuckDB warehouse sizes under data/.
    wh_bytes = 0
    try:
        data_root = Path(__file__).resolve().parent.parent / "data"
        if data_root.is_dir():
            for p in data_root.rglob("warehouse.duckdb"):
                try:
                    wh_bytes += p.stat().st_size
                except OSError:
                    pass
    except Exception:
        wh_bytes = 0
    if wh_bytes >= 1_000_000_000:
        wh_label = f"{wh_bytes / 1_000_000_000:.1f} GB"
    elif wh_bytes >= 1_000_000:
        wh_label = f"{wh_bytes / 1_000_000:.0f} MB"
    elif wh_bytes > 0:
        wh_label = f"{wh_bytes / 1_000:.0f} KB"
    else:
        wh_label = "—"
    rows.append({"label": "Warehouse (DuckDB)", "value": wh_label, "ok": True})

    # Stale .running markers across products.
    marker_ok = True
    marker_value = "none"
    try:
        from datetime import datetime as _dt, timezone as _tz
        data_root = Path(__file__).resolve().parent.parent / "data"
        oldest_age_h = None
        if data_root.is_dir():
            now = _dt.now(_tz.utc)
            for marker in data_root.rglob(".running"):
                try:
                    mtime = _dt.fromtimestamp(marker.stat().st_mtime, tz=_tz.utc)
                    age_h = (now - mtime).total_seconds() / 3600.0
                    if oldest_age_h is None or age_h > oldest_age_h:
                        oldest_age_h = age_h
                except OSError:
                    pass
        if oldest_age_h is not None:
            if oldest_age_h >= 24:
                marker_ok = False
                marker_value = f"stale {oldest_age_h / 24:.0f}d"
            elif oldest_age_h >= 2:
                marker_ok = False
                marker_value = f"stale {oldest_age_h:.0f}h"
            else:
                marker_value = "active"
    except Exception:
        marker_value = "—"
    rows.append({"label": "Run marker", "value": marker_value, "ok": marker_ok})

    # Disk free on the install root.
    disk_ok = True
    disk_value = "—"
    try:
        usage = shutil.disk_usage(Path(__file__).resolve().parent.parent)
        free_gb = usage.free / (1024 ** 3)
        disk_value = f"{free_gb:.0f} GB"
        disk_ok = free_gb >= 2.0
    except Exception:
        pass
    rows.append({"label": "Disk free", "value": disk_value, "ok": disk_ok})

    return {"rows": rows}


def _admin_features_spend_30d() -> dict:
    """30-day spend summary + top stages for the admin features right rail."""
    out: dict = {
        "cost": 0.0,
        "cost_fmt": "$0.00",
        "tokens": 0,
        "tokens_fmt": "0",
        "stages": [],
    }
    try:
        from datetime import datetime as _dt, timedelta, timezone as _tz
        from pipeline import token_usage as _tu

        until = _dt.now(_tz.utc)
        since = until - timedelta(days=30)
        by_stage = _tu.cross_product_totals(since, until, group_by=("stage",))
        totals = by_stage.get("totals") or {}
        cost = float(totals.get("cost_usd") or 0.0)
        tokens = int(totals.get("tokens") or 0)
        out["cost"] = cost
        out["cost_fmt"] = f"${cost:,.2f}"
        out["tokens"] = tokens
        if tokens >= 1_000_000:
            out["tokens_fmt"] = f"{tokens / 1_000_000:.2f}M".rstrip("0").rstrip(".")
        elif tokens >= 1_000:
            out["tokens_fmt"] = f"{tokens / 1_000:.1f}k".rstrip("0").rstrip(".")
        else:
            out["tokens_fmt"] = str(tokens)

        # Collapse to relevance / classify / digest buckets for the bar.
        buckets = {"relevance": 0.0, "classify": 0.0, "digest": 0.0}
        for row in by_stage.get("series") or []:
            stage = (row.get("stage") or "").lower()
            c = float(row.get("cost_usd") or 0.0)
            if stage == "relevance":
                buckets["relevance"] += c
            elif stage == "classify":
                buckets["classify"] += c
            else:
                buckets["digest"] += c
        out["stages"] = [
            {"label": k, "cost": v, "cost_fmt": f"${v:,.2f}"}
            for k, v in buckets.items()
            if v > 0 or cost == 0
        ]
        if cost > 0:
            out["stages"] = [s for s in out["stages"] if s["cost"] > 0]
    except Exception:
        pass
    return out


@router.get("/admin/features", response_class=HTMLResponse)
def admin_features(request: Request, saved: int = 0, error: Optional[str] = None):
    """List every declared flag with its current global value + phase label.

    Also surfaces the current assistant-LLM configuration so admins have one
    place to see feature-flag state alongside the connection that powers the
    flagged features (wizard drafting, snippet candidates, prompt suggestions).
    """
    from pipeline import assistant_llm as _al
    from pipeline import features as _features

    all_flags = _features.all_flags()
    # Stable section order for a general audience (not build-phase numbering).
    _section_order = (
        "Product setup",
        "AI helper",
        "Sources & plugins",
        "Reports",
        "Training & quality",
        "Runs & notifications",
        "Admin tools",
        "Other",
    )
    buckets: dict[str, list[dict]] = {s: [] for s in _section_order}
    for name, value in sorted(all_flags.items()):
        phase = _PHASE_ORDER.get(name, "Other")
        ui = _FEATURE_UI.get(name) or {}
        buckets.setdefault(phase, []).append({
            "name": name,
            "display": ui.get("display") or name.replace("_", " ").title(),
            "value": bool(value),
            "help": ui.get(
                "help",
                "Turns this optional capability on or off for every product.",
            ),
        })
    grouped = {k: v for k, v in buckets.items() if v}

    cfg = _al.current_config()
    assistant_summary = {
        "configured": cfg is not None,
        "endpoint": cfg.endpoint if cfg else "",
        "model": cfg.model if cfg else "",
        "budget_usd": cfg.budget_usd_per_product_per_month if cfg else 0.0,
        "api_key_env": cfg.api_key_env if cfg else "",
        "enabled": _features.enabled("assistant_llm_enabled"),
    }

    # Right-rail panels (Obsidian Console / ADR-0032) — best-effort from
    # existing llm_usage + filesystem probes; never fail the page.
    install_health = _admin_features_install_health(assistant_summary)
    spend_30d = _admin_features_spend_30d()
    flags_on = sum(1 for flags in grouped.values() for f in flags if f["value"])

    return core.templates.TemplateResponse(
        "admin_features.html",
        {
            "request": request,
            "grouped": grouped,
            "yaml_path": str(core._FEATURES_YAML),
            "saved": bool(saved),
            "error": error,
            "assistant_summary": assistant_summary,
            "install_health": install_health,
            "spend_30d": spend_30d,
            "admin_flags_on": flags_on,
            "admin_active": "features",
        },
    )


@router.post("/admin/features")
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
        current = yaml.safe_load(core._FEATURES_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        current = {}
    current["features"] = new_map

    tmp = core._FEATURES_YAML.with_suffix(core._FEATURES_YAML.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(current, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    if core._FEATURES_YAML.exists():
        backup = core._FEATURES_YAML.with_suffix(core._FEATURES_YAML.suffix + ".bak")
        core._FEATURES_YAML.replace(backup)
    tmp.replace(core._FEATURES_YAML)

    _features.clear_cache()
    return RedirectResponse(url="/admin/features?saved=1", status_code=303)


@router.get("/admin/system", response_class=HTMLResponse)
def admin_system(request: Request):
    """Consolidated install-wide health view for the Admin sidebar.

    Pulls the panels that used to be scattered across `/` (attention,
    source health, install summary) and `/admin/features` (install health,
    30d spend) into one page so the sidebar's "System health" link points
    somewhere real.
    """
    from pipeline import assistant_llm as _al
    from pipeline import features as _features
    from webui.services import dashboard as _dash

    products = []
    for pid in available_products():
        try:
            p = load_product(pid)
            products.append({"id": p.id, "display": p.display})
        except Exception as e:
            products.append({"id": pid, "display": pid, "error": str(e)})

    dash_ctx = _dash.build_index_context(products, [])

    cfg = _al.current_config()
    assistant_summary = {
        "configured": cfg is not None,
        "endpoint": cfg.endpoint if cfg else "",
        "model": cfg.model if cfg else "",
        "budget_usd": cfg.budget_usd_per_product_per_month if cfg else 0.0,
        "api_key_env": cfg.api_key_env if cfg else "",
        "enabled": _features.enabled("assistant_llm_enabled"),
    }
    install_health = _admin_features_install_health(assistant_summary)
    spend_30d = _admin_features_spend_30d()

    return core.templates.TemplateResponse(
        "admin_system.html",
        {
            "request": request,
            "install_stats": dash_ctx["install_stats"],
            "attention": dash_ctx["attention"],
            "source_health": dash_ctx["source_health"],
            "install_health": install_health,
            "spend_30d": spend_30d,
            "assistant_summary": assistant_summary,
            "admin_active": "system",
        },
    )


@router.post("/admin/media-sources/pause")
def media_source_toggle_pause(feed_url: str = Form(...), paused: str = Form(...)):
    """Toggle the global paused flag on one publications-catalog entry."""
    from pipeline import media_sources as _media
    is_paused = paused.lower() in ("true", "1", "on", "yes")
    _media.set_paused(feed_url, is_paused)
    return RedirectResponse(url="/connections", status_code=303)


@router.get("/admin/media-sources/edit", response_class=HTMLResponse)
def media_source_edit_form(request: Request, feed_url: str):
    from pipeline import media_sources as _media
    entry = next((e for e in _media.load() if e["feed_url"] == feed_url), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="publication not found in catalog")
    return core.templates.TemplateResponse(
        "media_source_edit.html",
        {"request": request, "entry": entry, "original_feed_url": feed_url,
         "admin_active": "sources"},
    )


@router.post("/admin/media-sources/edit")
def media_source_edit_save(
    original_feed_url: str = Form(...),
    name: str = Form(...),
    domain: str = Form(""),
    feed_url: str = Form(...),
):
    from pipeline import media_sources as _media
    ok = _media.update_entry(original_feed_url, name=name, domain=domain,
                             new_feed_url=feed_url)
    if not ok:
        raise HTTPException(status_code=404, detail="publication not found in catalog")
    return RedirectResponse(url="/connections", status_code=303)
