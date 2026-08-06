"""Guided product setup wizard (POST_V1_PLAN §4.3).

7-step flow producing a working product directory end-to-end without
requiring manual YAML editing. Each LLM-assisted step calls the
assistant LLM (§4.8) for a first-draft, which the user then edits and
confirms.

Draft state persists at `products/.wizard_drafts/<slug>.yaml` so users
can leave the wizard and resume. When the final step is confirmed, the
draft is materialised into a real product directory via the existing
`scaffold_product` code path.

Iteration cap: **3 regenerations per LLM-assist step** — after 3
"regenerate" clicks in one step, the user must edit the current draft
or accept it (D from review, prevents infinite loops).

Feature-flagged by `features.wizard_enabled`. When off, the /products
create form is a plain scaffold call (existing behavior).

The wizard supports two entry points:
  1. Fresh — 7 steps
  2. Clone — copy taxonomy + prompts + snippets from an existing product,
             then the wizard only prompts for identity + minor edits.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


WIZARD_STEPS = [
    ("identity",    "Product identity"),
    ("scope",       "Scope statement"),
    ("taxonomy",    "Areas + features"),
    ("prompts",     "Prompt templates"),
    ("sources",     "Data sources"),
    ("llm",         "LLM connection"),
    ("snippets",    "Seed snippets"),
]
STEP_IDS = [s[0] for s in WIZARD_STEPS]

LLM_ASSISTED_STEPS = {"scope", "taxonomy", "prompts", "snippets"}

MAX_REGENERATIONS_PER_STEP = 3


# ---------------------------------------------------------------------------
# Keyless-first source defaults (first_run_solution.md §4.1)
# ---------------------------------------------------------------------------
#
# The wizard pre-populates draft.sources with sources that require no
# credentials so the user's first run works with zero setup. Keyed sources
# (Reddit, GitHub, Stack Exchange, YouTube) are surfaced separately on the
# sources step with an honest cost estimate ("~10 min, needs a script app").

KEYLESS_SOURCE_TYPES = ("hn", "rss", "microsoft_community", "apple_appstore")

# Ordered list of well-known keyed sources with their real setup cost. Rendered
# under a "more coverage (optional)" heading on the sources step.
KEYED_SOURCE_HINTS = [
    ("reddit",         "Reddit",           "~10 min — create a script-type app at reddit.com/prefs/apps"),
    ("github_issues",  "GitHub Issues",    "~5 min — personal access token with `public_repo` scope"),
    ("stackex",        "Stack Exchange",   "~5 min — register at stackapps.com for an API key"),
    ("youtube_comments", "YouTube Comments", "~10 min — Google Cloud project + YouTube Data API v3 key"),
    ("producthunt",    "Product Hunt",     "~10 min — OAuth application at producthunt.com/v2/oauth"),
]


def keyless_default_sources(slug: str, display: str) -> list[dict[str, Any]]:
    """Return a starter sources.yaml `sources:` list with keyless-only sources.

    Seeded with an HN search stream keyed on the product display name, since
    HN's Algolia API works out of the box for any query. Users can add or
    remove sources on the product's Sources page later, or here on the
    wizard's sources step.
    """
    return [
        {
            "id": "hn",
            "type": "hn",
            "credibility_weight": 1.0,
            "streams": [
                {
                    "name": f"hn-{slug}",
                    "search_queries": [display],
                    "include_tags": ["story"],
                    "max_pages_per_query": 3,
                    "hits_per_page": 50,
                },
            ],
        },
    ]


# ---------------------------------------------------------------------------
# LLM chooser step (first_run_solution.md §4.2)
# ---------------------------------------------------------------------------
#
# Users pick one of three paths on the wizard's LLM step:
#   1. "hosted"  — paste an API key for Anthropic / OpenAI / Gemini / Azure.
#                   We map the choice to the right env var name via
#                   PROVIDER_PRESETS below, so `_resolve_api_key` picks it up.
#   2. "ollama"  — point at a local Ollama endpoint.
#   3. "skip"    — leave LLM unconfigured. Pipeline runs fetch/normalize/filter
#                   only; report banner tells the user how to unlock the rest.

LLM_CHOICE_HOSTED = "hosted"
LLM_CHOICE_OLLAMA = "ollama"
LLM_CHOICE_SKIP = "skip"
LLM_CHOICES = (LLM_CHOICE_HOSTED, LLM_CHOICE_OLLAMA, LLM_CHOICE_SKIP)


PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "anthropic": {
        "display": "Anthropic (Claude)",
        "endpoint": "https://api.anthropic.com/v1/",
        "model": "claude-sonnet-4-6",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "openai": {
        "display": "OpenAI",
        "endpoint": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY",
    },
    "gemini": {
        "display": "Google Gemini",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-1.5-pro",
        "api_key_env": "GOOGLE_API_KEY",
    },
    "azure_openai": {
        "display": "Azure OpenAI",
        "endpoint": "https://YOUR-RESOURCE.openai.azure.com/openai/deployments/YOUR-DEPLOYMENT",
        "model": "gpt-4o",
        "api_key_env": "AZURE_OPENAI_API_KEY",
    },
}


DEFAULT_OLLAMA_ENDPOINT = "http://localhost:11434/v1"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"


def build_llm_routing(draft: "WizardDraft") -> Optional[dict[str, Any]]:
    """Materialize the wizard's LLM choice into a two-role llm_routing.yaml
    blob. Returns None when the user chose to skip (so we leave the scaffold's
    default routing in place)."""
    choice = draft.llm_choice or LLM_CHOICE_SKIP
    if choice == LLM_CHOICE_SKIP:
        return None
    if choice == LLM_CHOICE_HOSTED:
        preset = PROVIDER_PRESETS.get(draft.llm_provider or "", {})
        endpoint = (draft.llm_endpoint or preset.get("endpoint") or "").strip()
        model = (draft.llm_model or preset.get("model") or "").strip()
        api_key_env = preset.get("api_key_env")
    else:  # ollama
        endpoint = (draft.llm_endpoint or DEFAULT_OLLAMA_ENDPOINT).strip()
        model = (draft.llm_model or DEFAULT_OLLAMA_MODEL).strip()
        api_key_env = None
    if not endpoint or not model:
        return None

    relevance = {
        "endpoint": endpoint, "model": model,
        "temperature": 0, "seed": 42,
        "timeout_seconds": 20, "max_retries": 3,
    }
    classify = {
        **relevance,
        "timeout_seconds": 60,
        "use_guided_decoding": True,
        "fallback_repair_attempts": 1,
    }
    if api_key_env:
        relevance["api_key_env"] = api_key_env
        classify["api_key_env"] = api_key_env
    return {"relevance": relevance, "classify": classify}


# ---------------------------------------------------------------------------
# Draft state
# ---------------------------------------------------------------------------


DRAFTS_DIRNAME = ".wizard_drafts"


@dataclass
class WizardDraft:
    """Everything the wizard has collected so far. Serialized to YAML."""

    slug: str
    display: str = ""
    description: str = ""
    industry: str = ""
    primary_goal: str = ""

    scope_in: str = ""
    scope_out: str = ""

    areas: list[dict[str, Any]] = field(default_factory=list)

    prompts: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    snippets: list[dict[str, Any]] = field(default_factory=list)

    # Wizard LLM chooser (§4.2). llm_choice ∈ {"hosted", "ollama", "skip"}.
    # For "hosted", llm_provider identifies which PROVIDER_PRESETS entry to
    # use; endpoint/model can be overridden per-draft if the user edits them.
    llm_choice: str = ""
    llm_provider: str = ""
    llm_endpoint: str = ""
    llm_model: str = ""
    llm_health_ok: Optional[bool] = None
    llm_health_message: str = ""

    # Cloned from this source product's config on entry, if any.
    cloned_from: Optional[str] = None

    # Iteration cap tracking. Keyed by step id.
    regenerations: dict[str, int] = field(default_factory=dict)

    created_at: str = ""
    updated_at: str = ""

    def can_regenerate(self, step: str) -> bool:
        return self.regenerations.get(step, 0) < MAX_REGENERATIONS_PER_STEP

    def note_regeneration(self, step: str) -> None:
        self.regenerations[step] = self.regenerations.get(step, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug, "display": self.display,
            "description": self.description, "industry": self.industry,
            "primary_goal": self.primary_goal,
            "scope_in": self.scope_in, "scope_out": self.scope_out,
            "areas": self.areas,
            "prompts": self.prompts, "sources": self.sources,
            "snippets": self.snippets, "cloned_from": self.cloned_from,
            "llm_choice": self.llm_choice, "llm_provider": self.llm_provider,
            "llm_endpoint": self.llm_endpoint, "llm_model": self.llm_model,
            "llm_health_ok": self.llm_health_ok,
            "llm_health_message": self.llm_health_message,
            "regenerations": self.regenerations,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WizardDraft":
        return cls(
            slug=d.get("slug", ""), display=d.get("display", ""),
            description=d.get("description", ""),
            industry=d.get("industry", ""),
            primary_goal=d.get("primary_goal", ""),
            scope_in=d.get("scope_in", ""), scope_out=d.get("scope_out", ""),
            areas=d.get("areas") or [],
            prompts=d.get("prompts") or {}, sources=d.get("sources") or [],
            snippets=d.get("snippets") or [],
            cloned_from=d.get("cloned_from"),
            llm_choice=d.get("llm_choice", ""),
            llm_provider=d.get("llm_provider", ""),
            llm_endpoint=d.get("llm_endpoint", ""),
            llm_model=d.get("llm_model", ""),
            llm_health_ok=d.get("llm_health_ok"),
            llm_health_message=d.get("llm_health_message", ""),
            regenerations=d.get("regenerations") or {},
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )


def slugify(text: str) -> str:
    """Normalize a display name into a filesystem-safe product slug."""
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-") or "product"


def _drafts_dir(products_dir: Path) -> Path:
    return products_dir / DRAFTS_DIRNAME


def draft_path(products_dir: Path, slug: str) -> Path:
    return _drafts_dir(products_dir) / f"{slug}.yaml"


def load_draft(products_dir: Path, slug: str) -> Optional[WizardDraft]:
    path = draft_path(products_dir, slug)
    if not path.exists():
        return None
    try:
        return WizardDraft.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except Exception:
        return None


def save_draft(products_dir: Path, draft: WizardDraft) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not draft.created_at:
        draft.created_at = now
    draft.updated_at = now
    _drafts_dir(products_dir).mkdir(parents=True, exist_ok=True)
    path = draft_path(products_dir, draft.slug)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(draft.to_dict(), sort_keys=False, allow_unicode=True,
                       default_flow_style=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def discard_draft(products_dir: Path, slug: str) -> None:
    path = draft_path(products_dir, slug)
    if path.exists():
        path.unlink()


def list_drafts(products_dir: Path) -> list[WizardDraft]:
    d = _drafts_dir(products_dir)
    if not d.exists():
        return []
    out = []
    for path in sorted(d.glob("*.yaml")):
        try:
            out.append(WizardDraft.from_dict(
                yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            ))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# Step navigation
# ---------------------------------------------------------------------------


def next_step(current: str) -> Optional[str]:
    """Return the next step id, or None on the last step."""
    try:
        i = STEP_IDS.index(current)
    except ValueError:
        return STEP_IDS[0]
    if i + 1 < len(STEP_IDS):
        return STEP_IDS[i + 1]
    return None


def prev_step(current: str) -> Optional[str]:
    try:
        i = STEP_IDS.index(current)
    except ValueError:
        return None
    return STEP_IDS[i - 1] if i > 0 else None


def is_valid_step(step: str) -> bool:
    return step in STEP_IDS


# ---------------------------------------------------------------------------
# Clone flow
# ---------------------------------------------------------------------------


def clone_from(
    source_dir: Path,
    slug: str,
    display: str,
    description: str,
) -> WizardDraft:
    """Populate a fresh draft from an existing product's config files.

    Copies taxonomy areas, prompts, and snippets. Sources and identity
    are NOT copied — those are inherently per-product.
    """
    draft = WizardDraft(slug=slug, display=display, description=description,
                        cloned_from=source_dir.name)

    def _load(name: str) -> dict:
        p = source_dir / name
        if not p.exists():
            return {}
        try:
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}

    taxonomy = _load("taxonomy.yaml")
    draft.areas = list(taxonomy.get("areas") or [])

    prompts = _load("prompts.yaml")
    # Strip version/id — new product gets fresh versioning.
    prompts.pop("id", None)
    prompts.pop("version", None)
    prompts.pop("updated_at", None)
    draft.prompts = prompts

    # Copy snippet YAMLs verbatim
    ex_dir = source_dir / "examples"
    if ex_dir.exists():
        for polarity_dir in ("positive", "negative"):
            pd = ex_dir / polarity_dir
            if not pd.exists():
                continue
            for fp in pd.glob("*.yaml"):
                try:
                    body = yaml.safe_load(fp.read_text(encoding="utf-8")) or {}
                except Exception:
                    continue
                body["_source_file"] = fp.name
                body["_polarity_dir"] = polarity_dir
                draft.snippets.append(body)

    return draft


# ---------------------------------------------------------------------------
# Finalization
# ---------------------------------------------------------------------------


def materialize(
    draft: WizardDraft,
    *,
    scaffold_fn,
    products_dir: Path,
) -> Path:
    """Turn the draft into a real product directory.

    `scaffold_fn(slug, display, description)` is `scaffold_product` from
    pipeline.product; injected so wizard.py doesn't take a hard dep on
    product.py that would make testing awkward.

    Overwrites taxonomy / prompts if the draft has content. Copies
    snippets into examples/<polarity>/. Sources go into sources.yaml.
    The draft is deleted on success.

    Raises FileExistsError if a product with that slug already exists.
    """
    scaffold_fn(draft.slug, draft.display, draft.description)
    product_dir = products_dir / draft.slug

    if draft.areas:
        _write_yaml(product_dir / "taxonomy.yaml", {"areas": draft.areas})
    if draft.prompts:
        _write_yaml(product_dir / "prompts.yaml", draft.prompts)
    if draft.sources:
        _write_yaml(product_dir / "sources.yaml", {"sources": draft.sources})

    routing = build_llm_routing(draft)
    if routing is not None:
        _write_yaml(product_dir / "llm_routing.yaml", routing)

    if draft.snippets:
        ex_dir = product_dir / "examples"
        for snip in draft.snippets:
            polarity_dir = snip.pop("_polarity_dir", None)
            src_file = snip.pop("_source_file", None)
            polarity = polarity_dir or (
                "positive" if snip.get("polarity") == "positive_example"
                else "negative"
            )
            target_dir = ex_dir / polarity
            target_dir.mkdir(parents=True, exist_ok=True)
            fname = src_file or f"{_snippet_filename(snip)}.yaml"
            (target_dir / fname).write_text(
                yaml.safe_dump(snip, sort_keys=False, allow_unicode=True,
                               default_flow_style=False),
                encoding="utf-8",
            )

    discard_draft(products_dir, draft.slug)
    return product_dir


def _snippet_filename(snip: dict[str, Any]) -> str:
    title = snip.get("title") or snip.get("body", "")[:40] or "snippet"
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "snippet"


def _write_yaml(path: Path, blob: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(blob, sort_keys=False, allow_unicode=True,
                       default_flow_style=False),
        encoding="utf-8",
    )
