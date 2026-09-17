"""Editable master prompt templates.

Each template drives one of the LLM calls the wizard or the product
scaffold makes. They start life as code defaults (below), and the admin
can override any of them by editing `config/prompt_templates.yaml` from
the Admin > Prompts tab in the UI.

Not to be confused with per-product prompts.yaml — those are the *result*
of applying these templates (the scaffold copies `scaffold_relevance_*`
into a new product's prompts.yaml; the assistant LLM sends the
`assistant_*` prompts during wizard drafting).

Reads are cheap (single small YAML) and cached; call `clear_cache()`
after saves. Override values in config/prompt_templates.yaml completely
replace the default — no merging. Missing keys fall back to the default.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from pipeline.config import CONFIG_DIR

_TEMPLATES_YAML = CONFIG_DIR / "prompt_templates.yaml"


# ---------------------------------------------------------------------------
# Template registry — every consumer names its template here.
#
# `stage` groups the display in the admin UI; `used_by` names the code
# reference so operators can trace how a template is consumed.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplateSpec:
    key: str            # config-file + save-form key
    display: str        # human-readable name in the admin UI
    stage: str          # bucket in the admin UI (Scaffold / Assistant LLM / Wizard v1)
    used_by: str        # code reference for operators
    purpose: str        # one-line description
    default: str        # code-level fallback


# --- Scaffold defaults (used by scaffold_product to seed a new product's
#     products/<id>/prompts.yaml). Editing here doesn't touch existing
#     products — only affects the NEXT product created.

_SCAFFOLD_RELEVANCE_SYSTEM = (
    "You are a strict relevance classifier. Reply with JSON only."
)

_SCAFFOLD_RELEVANCE_TEMPLATE = """Is this post about {product_display}?

{few_shot_block}
Reply with a single JSON object: {{"relevant": true|false, "confidence": 0.0-1.0}}

Title: {title}
Body: {body}
"""

_SCAFFOLD_CLASSIFY_SYSTEM = """You are classifying user feedback about {product_display}.
Return only JSON matching the requested schema. Use multi-label where
applicable. Use "unknown" or null rather than guessing.
"""

_SCAFFOLD_CLASSIFY_TEMPLATE = """Read the post and return JSON matching the schema.

ENABLED AREAS (multi-select; use the id):
{areas}

FEATURES (within each area, specific things to look for — use these
descriptions to decide which areas to tag):
{features}

CONTENT TYPES (multi-select):
{content_types}

If you tag bug_report, fill bug_* including repro_steps extracted verbatim
if present (else null and bug_repro_steps_quality="none").
If you tag feature_request, fill request_*.

CHURN DETECTION (independent of content_type). Set `churn_signal: true` when
the author signals they are leaving, considering leaving, or actively
steering others away from the product; else `false`. When true, set
`churn_reason` to one of:
  switching_to_alternative       (explicit move to a named alternative)
  canceling_subscription         (cancels a paid tier / uninstalls)
  considering_alternatives       (evaluating but not committed yet)
  stopped_using                  (already left, may not name a replacement)
  active_recommendation_against  ("don't use X" without naming own move)
When `churn_signal: false`, set `churn_reason: null`.
{extras_instructions}

For each entity assign:
  type (controlled vocab), product, version, role, confidence (0-1), verbatim.
  role: feature_implicated (user blames it) | hardware_in_use | software_in_use.

{few_shot_block}
REGEX PRE-PASS HINTS (confirm/correct, add what was missed, discard false positives):
  build numbers: {build_numbers}
{parent_block}
POST:
TITLE: {title}
BODY: {body}
ENGAGEMENT: {engagement}
SOURCE: {source}

Return ONLY valid JSON.
"""


# --- Assistant LLM prompts (sent by wizard-time LLM calls).

_ASSISTANT_PROFILE_DRAFT = (
    "You are helping a product team set up automated monitoring of public "
    "customer feedback about their product. Given the product's name, an "
    "optional web page describing it, and the team's goals, draft a profile "
    "the team will confirm.\n\n"
    "Rules:\n"
    "- Description: 2-4 sentences, <= 120 words, third-person, product-focused.\n"
    "- aliases: names the product is genuinely known by. Do NOT invent aliases "
    "if the source material doesn't support them.\n"
    "- not_to_be_confused_with: similarly-named products the classifier could "
    "mistake for the target. 0-5 items.\n"
    "- competitors: names only. 0-6 items.\n"
    "- scope_in: 2-4 short bullets naming concrete topics in scope.\n"
    "- scope_out: 1-3 short bullets naming concrete topics to exclude.\n"
    "- suggested_sources: 2-6 items. Prefer keyless sources first "
    "(hn, rss, microsoft_community, apple_appstore). Include one or two "
    "keyed sources (reddit, github_issues, stackex, youtube_comments, "
    "producthunt) only if the goals justify them. Each item MUST use a "
    "plugin_id from this allowed list — do NOT invent new ids.\n\n"
    "Return ONLY JSON matching the response schema. Never include prose."
)

_ASSISTANT_TAXONOMY_PROPOSAL = (
    "You design a taxonomy for a customer-feedback classifier. Areas are "
    "top-level buckets (like 'audio', 'search-quality', 'checkout'). Given a "
    "sample of real posts about the product and the product's confirmed "
    "profile facts, propose 3-5 areas that would let a reader group real "
    "feedback usefully.\n\n"
    "Rules:\n"
    "- 3-5 areas total.\n"
    "- Each area: snake_case id, short display, one-line description, "
    "3-8 keywords (single words or short phrases), and 1-3 example_item_ids "
    "picked from the sample.\n"
    "- Match the product's scope_in / scope_out; do not invent areas the "
    "sample doesn't support.\n"
    "- No IN/OUT scope essays. Descriptions are one line.\n\n"
    "Return ONLY JSON matching the schema."
)


# --- Wizard v1 assistant prompts (legacy, but still active behind
#     `wizard_enabled`). Kept so operators can see all wizard-facing
#     prompts in one place.

_ASSISTANT_V1_SCOPE = (
    "You help a product team define the scope of their customer-feedback "
    "monitoring topic. Given a short product description, produce a clear "
    "in-scope statement and an out-of-scope statement. Both should be "
    "concrete and useful for humans reviewing borderline items later."
)

_ASSISTANT_V1_TAXONOMY = (
    "You design a taxonomy (list of areas and features) for a customer-"
    "feedback classifier. Areas are top-level buckets like 'audio' or "
    "'search'; each has 2-5 features drilling in further. Ids should be "
    "snake_case and unique. Match the user's product intent, not generic "
    "categories."
)

_ASSISTANT_V1_PROMPTS = (
    "You draft LLM prompt templates for a customer-feedback classifier. "
    "Produce a `relevance` prompt (is this post about the product?) and a "
    "`classify` prompt (assign areas, content types, sentiment, entities). "
    "Use exactly the placeholder names in the schema; do not invent new ones."
)

_ASSISTANT_V1_SNIPPETS = (
    "You compose seed snippets for a customer-feedback classifier. Each "
    "snippet should be realistic — the kind of post a real user might "
    "write. Mix positive_example (in scope) with negative_example "
    "(off-topic, tests the relevance gate). Include diverse areas."
)


# --- Digest v2 headline pass (ADR 0016 §5.3; report_v2_design.md §5.3).
# Live generation lands in Slice 3b; templates registered here so admins can
# tune the wording via /admin/prompts immediately.

_ASSISTANT_DIGEST_HEADLINE_SYSTEM = (
    "You write one-sentence headlines that summarize a customer-feedback item "
    "for a digest table row. Neutral and factual — no adjectives, no "
    "editorializing. Preserve product names verbatim. Target "
    "12-18 words. No trailing period."
)

_ASSISTANT_DIGEST_HEADLINE_TEMPLATE = (
    "Rewrite the following user-feedback item as a single-line headline "
    "(12-18 words) for a digest row. Preserve the specific problem or "
    "request stated in the source. Don't invent details not present in "
    "the source.\n\n"
    "SOURCE: {source_display_name}\n"
    "TITLE: {title}\n"
    "BODY: {body}\n"
    "EXISTING SUMMARY (may be blank): {summary}\n\n"
    "Return ONLY the headline text, nothing else."
)


TEMPLATES: dict[str, TemplateSpec] = {t.key: t for t in [
    # Scaffold defaults — copied into new products' prompts.yaml.
    TemplateSpec(
        key="scaffold_relevance_system",
        display="Scaffold — relevance system",
        stage="Product scaffold defaults",
        used_by="pipeline/product.py::scaffold_product",
        purpose="System message copied into new products' prompts.yaml at "
                 "creation time. The relevance stage prepends this to each "
                 "LLM call.",
        default=_SCAFFOLD_RELEVANCE_SYSTEM,
    ),
    TemplateSpec(
        key="scaffold_relevance_template",
        display="Scaffold — relevance user template",
        stage="Product scaffold defaults",
        used_by="pipeline/product.py::scaffold_product",
        purpose="User-message template for the relevance stage. Placeholders: "
                 "{product_display}, {title}, {body}, {few_shot_block}, "
                 "{product_description}.",
        default=_SCAFFOLD_RELEVANCE_TEMPLATE,
    ),
    TemplateSpec(
        key="scaffold_classify_system",
        display="Scaffold — classify system",
        stage="Product scaffold defaults",
        used_by="pipeline/product.py::scaffold_product",
        purpose="System message for the classify stage. Copied into new "
                 "products' prompts.yaml.",
        default=_SCAFFOLD_CLASSIFY_SYSTEM,
    ),
    TemplateSpec(
        key="scaffold_classify_template",
        display="Scaffold — classify user template",
        stage="Product scaffold defaults",
        used_by="pipeline/product.py::scaffold_product",
        purpose="User-message template for the classify stage. Many "
                 "placeholders: {areas}, {features}, {content_types}, "
                 "{build_numbers}, "
                 "{extras_instructions}, {few_shot_block}, {parent_block}, "
                 "{title}, {body}, {engagement}, {source}.",
        default=_SCAFFOLD_CLASSIFY_TEMPLATE,
    ),
    # Assistant LLM (wizard v2 drafting + taxonomy proposal).
    TemplateSpec(
        key="assistant_profile_draft",
        display="Assistant — profile drafting",
        stage="Assistant LLM (wizard v2)",
        used_by="pipeline/profile_draft.py::draft_profile",
        purpose="Wizard Screen 1 → 2. Turns product name + URL/description "
                 "into a drafted profile (description, aliases, scope, "
                 "competitors, suggested sources).",
        default=_ASSISTANT_PROFILE_DRAFT,
    ),
    TemplateSpec(
        key="assistant_taxonomy_proposal",
        display="Assistant — taxonomy proposal",
        stage="Assistant LLM (wizard v2)",
        used_by="pipeline/taxonomy_proposal.py::propose_taxonomy",
        purpose="Wizard Screen 3 → 4. Clusters the minifetch corpus into "
                 "3-5 areas with keywords and example items.",
        default=_ASSISTANT_TAXONOMY_PROPOSAL,
    ),
    # Wizard v2 — per-stream identifier suggestions (subreddit, feed_url, etc.)
    TemplateSpec(
        key="assistant_stream_suggestions",
        display="Assistant — per-stream identifier suggestions",
        stage="Assistant LLM (wizard v2)",
        used_by="pipeline/stream_suggestions.py::suggest_stream_identifiers",
        purpose="Wizard Step 3 (Choose sources). For sources that need a "
                 "per-stream identifier the wizard can't invent — subreddit, "
                 "feed_url, apple app_id, github repo, etc. — the assistant "
                 "suggests likely values based on the product profile. User "
                 "picks from checkboxes + can add more inline.",
        default=(
            "You suggest concrete per-stream identifiers for a customer-"
            "feedback monitoring pipeline. Given a product profile and a "
            "source-plugin id + field name, propose 3-8 identifiers that "
            "would surface real user feedback about THIS SPECIFIC product.\n\n"
            "OUTPUT SHAPE: an array of {value, rationale}. `value` is the "
            "machine identifier the pipeline sends downstream (a subreddit "
            "name, a URL, an app id, etc.). `rationale` is a SHORT human "
            "label the wizard shows in a checklist — see per-plugin rules "
            "below for what belongs there.\n\n"
            "Rules per plugin:\n\n"
            "- reddit / scrapecreators_reddit (subreddit):\n"
            "  * value = plain subreddit name (no `r/`)\n"
            "  * rationale = one line on why this sub is relevant\n"
            "  * Prefer product-specific subs; add adjacent communities only "
            "if likely useful.\n\n"
            "- microsoft_community (feed_url):\n"
            "  * value = full RSS URL of the form "
            "https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/"
            "Category?category.id=<Name>\n"
            "  * rationale = the category name in plain English + why it "
            "matches (e.g. \"Windows category — main Windows discussion\")\n"
            "  * Common categories: Windows, Microsoft365, Azure, Office, "
            "SharePoint, WindowsInsider, OfficeInsider, Exchange, Teams.\n\n"
            "- rss (feed_url) — GENERAL RSS FEEDS ONLY (news, blogs, "
            "industry press, product-adjacent forums):\n"
            "  * value = full RSS/Atom URL\n"
            "  * rationale = the publication/blog name + why (e.g. "
            "\"The Verge — mainstream tech news often covers Windows\")\n"
            "  * DO NOT suggest reddit RSS URLs here — reddit content "
            "belongs on the `reddit` plugin, not the generic `rss` plugin.\n"
            "  * DO NOT suggest microsoft_community RSS URLs here — that's "
            "the `microsoft_community` plugin's job.\n\n"
            "- apple_appstore (app_id) — CRITICAL:\n"
            "  * value = the numeric app id from apps.apple.com/…/id{THIS}\n"
            "  * rationale = the app title + short reason (e.g. "
            "\"Netflix — flagship streaming app, direct competitor\")\n"
            "  * ONLY real, verifiable ids. If you don't know an app's "
            "actual id, DROP the entry — never invent numbers. Users are "
            "shown the rationale (title) prominently and the numeric id in "
            "small type, so a wrong id will point at the wrong app.\n\n"
            "- github_issues (repos):\n"
            "  * value = `owner/repo` string\n"
            "  * rationale = what this repo tracks (e.g. \"microsoft/vscode — "
            "official VS Code repo, users file feedback as issues\")\n\n"
            "- stackex (site):\n"
            "  * value = stackexchange site slug (stackoverflow, "
            "superuser, apple, askubuntu, gaming, etc.)\n"
            "  * rationale = the site's focus (e.g. \"Super User — Windows/"
            "Mac end-user Q&A\")\n\n"
            "- scrapecreators_x (handle):\n"
            "  * value = X/Twitter handle without `@`\n"
            "  * rationale = who this account is\n\n"
            "- scrapecreators_tiktok (username):\n"
            "  * value = TikTok username without `@`\n"
            "  * rationale = who this account is\n\n"
            "- youtube_comments (search_queries):\n"
            "  * value = a search query YouTube surfaces relevant videos for\n"
            "  * rationale = one line on what kind of videos this catches\n\n"
            "If NO good matches exist for this product, return an empty "
            "list — never invent.\n\n"
            "Return ONLY JSON matching the schema."
        ),
    ),
    # ADR-0030: Stream auto-discovery — LLM-generated search queries
    # that feed the provider-native search inside each plugin's
    # discover_streams(). Different shape than assistant_stream_suggestions:
    # this prompt returns SEARCH TERMS, not identifiers. Provider search
    # then produces the identifiers.
    TemplateSpec(
        key="assistant_stream_search_queries",
        display="Assistant — stream-discovery search queries",
        stage="Assistant LLM (stream auto-discovery)",
        used_by="pipeline/stream_query_generation.py::generate_search_queries",
        purpose=(
            "Stream auto-discovery (ADR-0030). Given a product profile "
            "and a source plugin id, generate 3-8 short search queries "
            "(3-4 words each) suitable for the plugin's provider-native "
            "search API (Reddit subreddit search, GitHub repo search, "
            "iTunes app search, etc.). The plugin's discover_streams() "
            "then runs each query against the provider and merges the "
            "resulting candidates into a ranked list."
        ),
        default=(
            "Generate short search queries for finding relevant streams "
            "on a specific source-plugin's provider search endpoint. "
            "Given a product profile + plugin_id, return 3-8 queries. "
            "Each query is 3-4 words. Queries should be specific enough "
            "that a keyword search on the provider returns tightly-"
            "scoped results — prefer product-specific terms and adjacent "
            "community names over generic industry terms.\n\n"
            "PLUGIN-SPECIFIC GUIDANCE:\n\n"
            "- reddit / reddit_rss / scrapecreators_reddit: queries feed "
            "Reddit's subreddit-search endpoint. Mix product-name-based "
            "queries with community-shape queries. E.g. for Notion:\n"
            "  * \"notion productivity software\"\n"
            "  * \"note taking app\"\n"
            "  * \"personal knowledge management\"\n"
            "  * \"productivity apps community\"\n"
            "  Prefer subreddit-name-like phrases. Skip generic terms "
            "like \"software\" alone.\n\n"
            "- github_issues: queries feed GitHub's repository search. "
            "Mix product-name queries with functional-adjacency queries. "
            "E.g. for a React library:\n"
            "  * \"react state management\"\n"
            "  * \"typescript react hooks\"\n\n"
            "- apple_appstore: queries feed iTunes app search. Prefer "
            "exact product name + close variants. E.g.:\n"
            "  * \"notion notes\"\n"
            "  * \"notion productivity\"\n\n"
            "- producthunt / youtube_comments: same pattern — provider-"
            "specific keywords likely to surface real content about the "
            "product.\n\n"
            "Bias toward queries that PROVIDER SEARCH will produce "
            "diverse, tightly-scoped results for. If a query is too "
            "broad (\"software\"), skip it. If a query hits mostly "
            "irrelevant results on the provider (\"productivity\" alone "
            "returns 500 subs), scope it tighter.\n\n"
            "Return ONLY a JSON array of strings. No prose, no keys, "
            "no rationale — just the query strings. Example output:\n"
            "  [\"windows 11 issues\", \"microsoft os updates\", "
            "\"pc gaming windows\", \"sysadmin windows\"]"
        ),
    ),
    # Wizard v1 (still active behind the legacy `wizard_enabled` flag).
    TemplateSpec(
        key="assistant_v1_scope",
        display="Wizard v1 — scope suggestion",
        stage="Wizard v1 (legacy)",
        used_by="pipeline/wizard_llm.py::suggest_scope",
        purpose="Legacy v1 wizard's scope step. Retained so operators can "
                 "still tweak it while v1 exists.",
        default=_ASSISTANT_V1_SCOPE,
    ),
    TemplateSpec(
        key="assistant_v1_taxonomy",
        display="Wizard v1 — taxonomy suggestion",
        stage="Wizard v1 (legacy)",
        used_by="pipeline/wizard_llm.py::suggest_taxonomy",
        purpose="Legacy v1 wizard's taxonomy step.",
        default=_ASSISTANT_V1_TAXONOMY,
    ),
    TemplateSpec(
        key="assistant_v1_prompts",
        display="Wizard v1 — prompt template drafting",
        stage="Wizard v1 (legacy)",
        used_by="pipeline/wizard_llm.py::suggest_prompts",
        purpose="Legacy v1 wizard's step that drafts the per-product "
                 "prompts.yaml. Meta: a prompt used to write prompts.",
        default=_ASSISTANT_V1_PROMPTS,
    ),
    TemplateSpec(
        key="assistant_v1_snippets",
        display="Wizard v1 — snippet seed drafting",
        stage="Wizard v1 (legacy)",
        used_by="pipeline/wizard_llm.py::suggest_snippets",
        purpose="Legacy v1 wizard's step that drafts seed snippets.",
        default=_ASSISTANT_V1_SNIPPETS,
    ),
    # Digest v2 — headline pass (ADR 0016 §5.3 / report_v2_design.md §5.3).
    # Live generation lands in Slice 3b; templates registered here so the
    # admin UI at /admin/prompts exposes them immediately per §7.5.
    TemplateSpec(
        key="assistant_digest_headline_system",
        display="Digest — headline system",
        stage="Assistant LLM (digest v2)",
        used_by="pipeline/digest/headlines.py::generate (Slice 3b)",
        purpose="System message for the digest's per-item headline pass. "
                 "The digest calls this per top-N group per section; results "
                 "are cached by content-hash so re-renders are free.",
        default=_ASSISTANT_DIGEST_HEADLINE_SYSTEM,
    ),
    TemplateSpec(
        key="assistant_digest_headline_template",
        display="Digest — headline user template",
        stage="Assistant LLM (digest v2)",
        used_by="pipeline/digest/headlines.py::generate (Slice 3b)",
        purpose="User-message template for the headline pass. Placeholders: "
                 "{source_display_name}, {title}, {body}, {summary}.",
        default=_ASSISTANT_DIGEST_HEADLINE_TEMPLATE,
    ),
]}


# ---------------------------------------------------------------------------
# Read + write
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _overrides() -> dict[str, str]:
    """Load the override YAML into a dict — one shot, cached."""
    if not _TEMPLATES_YAML.exists():
        return {}
    try:
        data = yaml.safe_load(_TEMPLATES_YAML.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    tpls = data.get("templates") or {}
    return {k: str(v) for k, v in tpls.items() if isinstance(v, str) or v is None}


def get(key: str) -> str:
    """Return the current value for a template key. Overrides win; falls
    back to the code-level default if the config file is missing or the
    key isn't present."""
    spec = TEMPLATES.get(key)
    if spec is None:
        raise KeyError(f"unknown prompt template key: {key!r}")
    override = _overrides().get(key)
    if override is not None and override.strip():
        return override
    return spec.default


def all_current() -> list[dict]:
    """Return list of {spec, current, is_overridden} for the admin UI."""
    overrides = _overrides()
    out = []
    for key, spec in TEMPLATES.items():
        raw_override = overrides.get(key)
        overridden = raw_override is not None and raw_override.strip() != ""
        out.append({
            "spec": spec,
            "current": raw_override if overridden else spec.default,
            "is_overridden": overridden,
        })
    return out


def save_overrides(new_values: dict[str, str]) -> None:
    """Write the override file atomically. Only keys that DIFFER from the
    code default get written — otherwise the file drifts + hides useful
    upstream updates (a future release changes the default → we still
    serve the stale user override even if the user thought they had
    reverted). If `new_values[k]` matches the default, that key is
    stripped from the override map.

    Windows race-safe like save_draft: randomized tmp suffix + retry."""
    import os, random, time
    cleaned: dict[str, str] = {}
    for key, value in new_values.items():
        spec = TEMPLATES.get(key)
        if spec is None:
            continue
        val = (value or "").strip()
        # Save unchanged from default as "no override" — keeps the file
        # tight and lets upstream default updates flow through.
        if not val or val == spec.default.strip():
            continue
        cleaned[key] = value
    _TEMPLATES_YAML.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump({"templates": cleaned}, sort_keys=False,
                              default_flow_style=False, allow_unicode=True)
    last_err: Optional[Exception] = None
    for attempt in range(6):
        tmp = _TEMPLATES_YAML.with_suffix(
            f"{_TEMPLATES_YAML.suffix}.{os.getpid()}.{random.randint(0, 1_000_000):06d}.tmp"
        )
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, _TEMPLATES_YAML)
            clear_cache()
            return
        except PermissionError as e:
            last_err = e
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            time.sleep(min(0.02 * (2 ** attempt), 0.5))
    raise PermissionError(
        f"could not save prompt templates to {_TEMPLATES_YAML} "
        f"after 6 retries (last: {last_err!r})"
    )


def revert(key: str) -> None:
    """Drop a single override so the code default takes effect again."""
    overrides = dict(_overrides())
    if key in overrides:
        overrides.pop(key)
        # Re-use save_overrides — it treats matching-default as revert so
        # we just pass the current overrides through (any key still there
        # is really an override).
        save_overrides(overrides)
    clear_cache()


def clear_cache() -> None:
    _overrides.cache_clear()
