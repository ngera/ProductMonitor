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
    ("vendors",     "Key vendors / products"),
    ("prompts",     "Prompt templates"),
    ("sources",     "Data sources"),
    ("snippets",    "Seed snippets"),
]
STEP_IDS = [s[0] for s in WIZARD_STEPS]

LLM_ASSISTED_STEPS = {"scope", "taxonomy", "vendors", "prompts", "snippets"}

MAX_REGENERATIONS_PER_STEP = 3


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
    vendors: list[dict[str, Any]] = field(default_factory=list)

    prompts: dict[str, Any] = field(default_factory=dict)
    sources: list[dict[str, Any]] = field(default_factory=list)
    snippets: list[dict[str, Any]] = field(default_factory=list)

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
            "areas": self.areas, "vendors": self.vendors,
            "prompts": self.prompts, "sources": self.sources,
            "snippets": self.snippets, "cloned_from": self.cloned_from,
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
            areas=d.get("areas") or [], vendors=d.get("vendors") or [],
            prompts=d.get("prompts") or {}, sources=d.get("sources") or [],
            snippets=d.get("snippets") or [],
            cloned_from=d.get("cloned_from"),
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

    Copies taxonomy areas, vendors, prompts, and snippets. Sources and
    identity are NOT copied — those are inherently per-product.
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

    vendors = _load("vendors.yaml")
    draft.vendors = list(vendors.get("vendors") or [])

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

    Overwrites taxonomy / vendors / prompts if the draft has content.
    Copies snippets into examples/<polarity>/. Sources go into
    sources.yaml. The draft is deleted on success.

    Raises FileExistsError if a product with that slug already exists.
    """
    scaffold_fn(draft.slug, draft.display, draft.description)
    product_dir = products_dir / draft.slug

    if draft.areas:
        _write_yaml(product_dir / "taxonomy.yaml", {"areas": draft.areas})
    if draft.vendors:
        _write_yaml(product_dir / "vendors.yaml", {"vendors": draft.vendors})
    if draft.prompts:
        _write_yaml(product_dir / "prompts.yaml", draft.prompts)
    if draft.sources:
        _write_yaml(product_dir / "sources.yaml", {"sources": draft.sources})

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
