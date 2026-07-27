"""Wizard v2 draft persistence (wizard redesign Phase 3).

Independent from `pipeline.wizard` (v1) so both wizards can coexist behind
their respective feature flags. Materialization into a real product moves to
`pipeline.wizard_v2.materialize()` in Phase 5.

Draft state machine — one of:
    describe  — Screen 1 collected, LLM draft in-flight or not yet run
    profile   — Screen 2 (edit drafted facts)
    calibrate — Screen 3 (Phase 4 — live mini-fetch judgment deck)
    review    — Screen 4 (Phase 5 — final review & run)

Draft files live at `products/.wizard_drafts/v2/<slug>.yaml`. The v2 subdir
keeps v1 and v2 drafts in separate namespaces even though both wizards
target the same product-slug space.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml


DRAFTS_V2_DIRNAME = ".wizard_drafts/v2"
MAX_REGENERATIONS_PER_SECTION = 3

# The state machine the router advances through.
# NOTE: order matters for `previous_step` navigation.
STEPS: tuple[str, ...] = ("describe", "profile", "sources", "calibrate", "review")


def previous_step(step: str) -> Optional[str]:
    """Return the step name before `step`, or None if `step` is the first."""
    try:
        i = STEPS.index(step)
    except ValueError:
        return None
    return STEPS[i - 1] if i > 0 else None

# Sections the user can regenerate independently on Screen 2.
REGEN_SECTIONS: tuple[str, ...] = (
    "description",
    "aliases",
    "not_to_be_confused_with",
    "competitors",
    "scope_in",
    "scope_out",
    "suggested_sources",
)


@dataclass
class WizardV2Draft:
    """Serialized shape of a wizard-v2 draft.

    Every field is optional-with-default so a draft file created on Screen 1
    can grow through Screens 2-4 without schema-migration ceremony. `step`
    drives the router — the wizard renders whatever screen `step` names.
    """

    slug: str
    display: str = ""
    step: str = "describe"

    # Screen 1 raw input
    url_or_description: str = ""
    goals: list[str] = field(default_factory=list)

    # Screen 2 drafted profile (mirrors ProfileDraft field names + adds notes)
    description: str = ""
    url: str = ""
    aliases: list[str] = field(default_factory=list)
    not_to_be_confused_with: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    scope_in: list[str] = field(default_factory=list)
    scope_out: list[str] = field(default_factory=list)
    suggested_sources: list[dict[str, Any]] = field(default_factory=list)

    # Screen 3+ state (populated later)
    calibration: dict[str, Any] = field(default_factory=dict)
    proposed_taxonomy: dict[str, Any] = field(default_factory=dict)

    # Per-stream identifier suggestions cached by (plugin_id, field_name).
    # Populated on entering Step 3 so the LLM call runs once per draft
    # (not on every page load). Shape:
    #   { "reddit": { "subreddit": [{"value": "...", "rationale": "..."}] } }
    stream_suggestions: dict[str, dict[str, list[dict[str, str]]]] = field(default_factory=dict)

    # Sub-phase within Step 3. "pick" = the checkbox screen where the
    # user chooses which sources to include; "configure" = the follow-up
    # screen where every checked source's per-stream identifiers get
    # collected (LLM suggestions + textarea). The user can move back and
    # forth between these two sub-phases.
    sources_substep: str = "pick"

    # Meta / diagnostics
    page_fetch_failed: bool = False
    fetched_chars: int = 0
    drafting_error: str = ""
    regenerations: dict[str, int] = field(default_factory=dict)
    cloned_from: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""

    # LLM chooser fields (reuse v1 semantics for Phase 5 UX consistency)
    llm_choice: str = ""
    llm_provider: str = ""
    llm_endpoint: str = ""
    llm_model: str = ""
    llm_health_ok: Optional[bool] = None
    llm_health_message: str = ""

    def can_regenerate(self, section: str) -> bool:
        if section not in REGEN_SECTIONS:
            return False
        return self.regenerations.get(section, 0) < MAX_REGENERATIONS_PER_SECTION

    def note_regeneration(self, section: str) -> None:
        self.regenerations[section] = self.regenerations.get(section, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        # Explicit field order for stable diffs. New fields go at the bottom.
        return {
            "slug": self.slug, "display": self.display, "step": self.step,
            "url_or_description": self.url_or_description, "goals": self.goals,
            "description": self.description, "url": self.url,
            "aliases": self.aliases,
            "not_to_be_confused_with": self.not_to_be_confused_with,
            "competitors": self.competitors,
            "scope_in": self.scope_in, "scope_out": self.scope_out,
            "suggested_sources": self.suggested_sources,
            "calibration": self.calibration,
            "proposed_taxonomy": self.proposed_taxonomy,
            "stream_suggestions": self.stream_suggestions,
            "sources_substep": self.sources_substep,
            "page_fetch_failed": self.page_fetch_failed,
            "fetched_chars": self.fetched_chars,
            "drafting_error": self.drafting_error,
            "regenerations": self.regenerations,
            "cloned_from": self.cloned_from,
            "llm_choice": self.llm_choice,
            "llm_provider": self.llm_provider,
            "llm_endpoint": self.llm_endpoint,
            "llm_model": self.llm_model,
            "llm_health_ok": self.llm_health_ok,
            "llm_health_message": self.llm_health_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WizardV2Draft":
        return cls(
            slug=d.get("slug", ""),
            display=d.get("display", ""),
            step=d.get("step", "describe"),
            url_or_description=d.get("url_or_description", ""),
            goals=list(d.get("goals") or []),
            description=d.get("description", ""),
            url=d.get("url", ""),
            aliases=list(d.get("aliases") or []),
            not_to_be_confused_with=list(d.get("not_to_be_confused_with") or []),
            competitors=list(d.get("competitors") or []),
            scope_in=list(d.get("scope_in") or []),
            scope_out=list(d.get("scope_out") or []),
            suggested_sources=list(d.get("suggested_sources") or []),
            calibration=dict(d.get("calibration") or {}),
            proposed_taxonomy=dict(d.get("proposed_taxonomy") or {}),
            stream_suggestions=dict(d.get("stream_suggestions") or {}),
            sources_substep=d.get("sources_substep") or "pick",
            page_fetch_failed=bool(d.get("page_fetch_failed", False)),
            fetched_chars=int(d.get("fetched_chars", 0)),
            drafting_error=d.get("drafting_error", ""),
            regenerations=dict(d.get("regenerations") or {}),
            cloned_from=d.get("cloned_from"),
            llm_choice=d.get("llm_choice", ""),
            llm_provider=d.get("llm_provider", ""),
            llm_endpoint=d.get("llm_endpoint", ""),
            llm_model=d.get("llm_model", ""),
            llm_health_ok=d.get("llm_health_ok"),
            llm_health_message=d.get("llm_health_message", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
        )


# ---------------------------------------------------------------------------
# Slug helpers — mirror wizard v1's rules so a draft slug is always
# safe to use as a filesystem path segment.
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slugify(text: str) -> str:
    """Normalize to a filesystem-safe slug. Never returns empty string."""
    slug = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return slug or "product"


def _sanitize_slug(slug: str) -> str:
    """Reject slug values that could escape the drafts dir."""
    if not slug or "/" in slug or "\\" in slug or slug in (".", "..") or ".." in slug:
        raise ValueError(f"invalid draft slug: {slug!r}")
    if slug != slugify(slug):
        raise ValueError(f"slug must be pre-slugified: {slug!r}")
    return slug


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def drafts_dir(products_dir: Path) -> Path:
    return products_dir / DRAFTS_V2_DIRNAME


def draft_path(products_dir: Path, slug: str) -> Path:
    return drafts_dir(products_dir) / f"{_sanitize_slug(slug)}.yaml"


def load_draft(products_dir: Path, slug: str) -> Optional[WizardV2Draft]:
    try:
        path = draft_path(products_dir, slug)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return WizardV2Draft.from_dict(data)
    except Exception:
        return None


def save_draft(products_dir: Path, draft: WizardV2Draft) -> None:
    """Atomic YAML write with retry — Windows Defender / AV routinely locks
    freshly-created files while it scans them, which surfaces as
    `PermissionError: [Errno 13]` on the `.tmp.write_text()` or `.replace()`
    call. We retry a handful of times with brief backoff and use a
    per-attempt suffix so a stuck previous `.tmp` doesn't block us."""
    import os, random, time
    _sanitize_slug(draft.slug)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not draft.created_at:
        draft.created_at = now
    draft.updated_at = now
    drafts_dir(products_dir).mkdir(parents=True, exist_ok=True)
    path = draft_path(products_dir, draft.slug)
    payload = yaml.safe_dump(
        draft.to_dict(), sort_keys=False,
        allow_unicode=True, default_flow_style=False,
    )

    last_err: Optional[Exception] = None
    for attempt in range(6):
        # Randomized tmp suffix so a stale `.tmp` from a prior crashed
        # attempt doesn't collide with ours.
        tmp = path.with_suffix(
            f"{path.suffix}.{os.getpid()}.{random.randint(0, 1_000_000):06d}.tmp",
        )
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
            return
        except PermissionError as e:
            last_err = e
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            # Exponential backoff: 20ms, 40ms, 80ms, ..., capped.
            time.sleep(min(0.02 * (2 ** attempt), 0.5))
    raise PermissionError(
        f"could not save draft to {path} after 6 retries "
        f"(last error: {last_err!r}). This usually means antivirus or "
        f"another process is holding the file open."
    )


def discard_draft(products_dir: Path, slug: str) -> None:
    try:
        path = draft_path(products_dir, slug)
    except ValueError:
        return
    if path.exists():
        path.unlink()


def list_drafts(products_dir: Path) -> list[WizardV2Draft]:
    d = drafts_dir(products_dir)
    if not d.exists():
        return []
    out: list[WizardV2Draft] = []
    for p in sorted(d.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            out.append(WizardV2Draft.from_dict(data))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# ProfileDraft -> WizardV2Draft merge helper
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Materialization — draft → live product on disk
# ---------------------------------------------------------------------------


def materialize(
    draft: WizardV2Draft,
    products_dir: Path,
    *,
    proposed_taxonomy_yaml: Optional[dict[str, Any]] = None,
) -> Path:
    """Turn a completed draft into a real product under products/<slug>/.

    Steps:
      1. Call scaffold_product(slug, display, description, facts=facts) so
         product.yaml carries the facts fields from Phase 1.
      2. Overwrite the scaffold's taxonomy.yaml with the approved proposal
         (falls back to whatever's on the draft in `proposed_taxonomy`).
      3. Overwrite sources.yaml with the enabled suggested sources.
      4. Seed vendors.yaml from the draft's competitors so the classifier's
         vendor pre-pass has something to match against.
      5. Write llm_routing.yaml when the LLM chooser was used.
      6. Write examples/*/*.yaml from calibration judgments.
      7. Validate via load_product; on any failure, ROLL BACK by deleting
         the freshly-created product dir and re-raising.
      8. Delete the wizard draft file.

    Returns the path to the created product directory.
    """
    from pipeline.product import (
        clear_cache, load_product, scaffold_product, save_product_facts,
    )

    slug = draft.slug
    facts = {
        "url": draft.url,
        "aliases": list(draft.aliases),
        "not_to_be_confused_with": list(draft.not_to_be_confused_with),
        "goals": list(draft.goals),
        "competitors": list(draft.competitors),
        "scope_in": list(draft.scope_in),
        "scope_out": list(draft.scope_out),
    }
    target = products_dir / slug
    try:
        # Redirect the module-global PRODUCTS_DIR only for the duration of
        # scaffold if the caller passed a non-default products_dir. Tests
        # already monkeypatch PRODUCTS_DIR so this is a no-op there.
        product_dir = scaffold_product(
            slug, draft.display or slug,
            description=(draft.description or "").strip(),
            facts=facts,
        )
    except FileExistsError:
        raise

    try:
        # 2) Taxonomy — use proposal if supplied, else the draft's stored one.
        tax = proposed_taxonomy_yaml or draft.proposed_taxonomy
        if tax and tax.get("areas"):
            (product_dir / "taxonomy.yaml").write_text(
                yaml.safe_dump(tax, sort_keys=False, allow_unicode=True,
                               default_flow_style=False),
                encoding="utf-8",
            )

        # 3) Sources — enabled + fetchable only.
        enabled_srcs = [
            s for s in draft.suggested_sources if s.get("enabled")
        ]
        if enabled_srcs:
            sources_yaml = {
                "sources": [
                    _source_entry(s, slug) for s in enabled_srcs
                ]
            }
            (product_dir / "sources.yaml").write_text(
                yaml.safe_dump(sources_yaml, sort_keys=False,
                               allow_unicode=True, default_flow_style=False),
                encoding="utf-8",
            )

        # 4) Vendors — seed from competitors.
        if draft.competitors:
            vendors_yaml = {
                "version": datetime.now(timezone.utc).date().isoformat(),
                "vendors": [
                    {"name": c, "products": []} for c in draft.competitors
                ],
            }
            (product_dir / "vendors.yaml").write_text(
                yaml.safe_dump(vendors_yaml, sort_keys=False,
                               allow_unicode=True, default_flow_style=False),
                encoding="utf-8",
            )

        # 5) LLM routing — only when chooser was used (hosted/ollama).
        routing = _build_llm_routing(draft)
        if routing is not None:
            (product_dir / "llm_routing.yaml").write_text(
                yaml.safe_dump(routing, sort_keys=False, allow_unicode=True,
                               default_flow_style=False),
                encoding="utf-8",
            )

        # 6) Snippets from calibration.
        _materialize_snippets(product_dir, draft)

        # 7) Validate — will raise ValueError / FileNotFoundError on any
        #    misshape so we can roll back.
        clear_cache()
        load_product(slug)

    except Exception:
        # Roll back: leave the tree the way we found it.
        import shutil
        shutil.rmtree(product_dir, ignore_errors=True)
        clear_cache()
        raise

    # 8) Delete the draft. Failure here is not fatal — the product exists.
    discard_draft(products_dir, slug)
    return product_dir


def _source_entry(suggested: dict[str, Any], slug: str) -> dict[str, Any]:
    """Turn a WizardV2Draft.suggested_sources item into a sources.yaml entry.

    Wizard Step 3 can collect multiple per-stream identifiers (multiple
    subreddits under Reddit, several RSS feeds, etc.). The first identifier
    lives directly on stream_config; extras are stashed under
    `_extra_streams` and get expanded into additional streams here.
    """
    plugin_id = suggested.get("plugin_id", "")
    stream_config = dict(suggested.get("stream_config") or {})
    stream_config.setdefault("name", f"{plugin_id}-{slug}")
    extras = stream_config.pop("_extra_streams", None) or []
    streams = [stream_config]
    for extra in extras:
        if not isinstance(extra, dict):
            continue
        cfg = dict(extra)
        cfg.setdefault("name", f"{plugin_id}-{slug}-{len(streams) + 1}")
        streams.append(cfg)
    return {
        "id": plugin_id,
        "type": plugin_id,
        "credibility_weight": 1.0,
        "streams": streams,
    }


def _build_llm_routing(draft: WizardV2Draft) -> Optional[dict[str, Any]]:
    """Materialize the LLM chooser selection into llm_routing.yaml shape.

    For every choice — including "skip" — this now returns a non-None
    routing dict. When the user picks skip, we emit routing with an empty
    endpoint so the pipeline's health check fails FAST with a clear
    "endpoint not configured" reason rather than trying to hit the
    scaffold's Foundry-Local default (which fails with a confusing
    "connection refused" for anyone who doesn't have Foundry Local
    running).

    Mirrors `pipeline.wizard.build_llm_routing()` (v1)."""
    from pipeline.wizard import (
        DEFAULT_OLLAMA_ENDPOINT, DEFAULT_OLLAMA_MODEL,
        LLM_CHOICE_HOSTED, LLM_CHOICE_OLLAMA, LLM_CHOICE_SKIP,
        PROVIDER_PRESETS,
    )
    choice = draft.llm_choice or LLM_CHOICE_SKIP
    if choice == LLM_CHOICE_SKIP:
        # Explicit "skip" routing — the pipeline sees endpoint="" and
        # short-circuits health check with a specific message.
        skip_cfg = {
            "endpoint": "", "model": "",
            "temperature": 0, "seed": 42,
            "timeout_seconds": 20, "max_retries": 3,
        }
        return {"relevance": skip_cfg, "classify": dict(skip_cfg)}
    if choice == "assistant":
        # Route through the global assistant-LLM connection. Endpoint +
        # model came from the picked option; api_key_env resolves to
        # ASSISTANT_LLM_API_KEY via the assistant-llm config.
        try:
            from pipeline import assistant_llm as _al
            cfg = _al.current_config()
            api_key_env = (cfg.api_key_env if cfg and cfg.api_key_env
                            else "ASSISTANT_LLM_API_KEY")
        except Exception:
            api_key_env = "ASSISTANT_LLM_API_KEY"
        endpoint = (draft.llm_endpoint or "").strip()
        model = (draft.llm_model or "").strip()
    elif choice == LLM_CHOICE_HOSTED:
        preset = PROVIDER_PRESETS.get(draft.llm_provider or "", {})
        endpoint = (draft.llm_endpoint or preset.get("endpoint") or "").strip()
        model = (draft.llm_model or preset.get("model") or "").strip()
        api_key_env = preset.get("api_key_env")
    elif choice == LLM_CHOICE_OLLAMA:
        endpoint = (draft.llm_endpoint or DEFAULT_OLLAMA_ENDPOINT).strip()
        model = (draft.llm_model or DEFAULT_OLLAMA_MODEL).strip()
        api_key_env = None
    else:
        # Unknown choice string — treat as skip so we don't emit garbage
        # routing that fails at pipeline time.
        return None
    if not endpoint or not model:
        return None
    relevance = {
        "endpoint": endpoint, "model": model,
        "temperature": 0, "seed": 42,
        "timeout_seconds": 20, "max_retries": 3,
    }
    classify = {
        **relevance, "timeout_seconds": 60,
        "use_guided_decoding": True, "fallback_repair_attempts": 1,
    }
    if api_key_env:
        relevance["api_key_env"] = api_key_env
        classify["api_key_env"] = api_key_env
    return {"relevance": relevance, "classify": classify}


def _materialize_snippets(product_dir: Path, draft: WizardV2Draft) -> None:
    """Persist non-skip calibration judgments as example YAML files.

    Uses the *existing* snippet file shape (matches `pipeline.snippets.save_snippet`)
    so few-shot loading and eval-split logic keep working unchanged.
    """
    judgments = (draft.calibration or {}).get("judgments") or {}
    if not judgments:
        return
    import re
    slug_re = re.compile(r"[^a-z0-9]+")
    ex_pos = product_dir / "examples" / "positive"
    ex_neg = product_dir / "examples" / "negative"
    ex_pos.mkdir(parents=True, exist_ok=True)
    ex_neg.mkdir(parents=True, exist_ok=True)
    for _item_id, entry in judgments.items():
        polarity = entry.get("polarity")
        if polarity not in ("positive_example", "negative_example"):
            continue
        target = ex_pos if polarity == "positive_example" else ex_neg
        slug = slug_re.sub("-", (entry.get("title") or "post").lower()).strip("-")[:60]
        if not slug:
            slug = entry.get("item_id", "post").replace(":", "-")[:60]
        blob = {
            "source_url": entry.get("source_url") or None,
            "title": entry.get("title") or None,
            "body": entry.get("body") or "",
            "polarity": polarity,
            "holdout_eval": False,
            "labels": {"is_topic_relevant": polarity == "positive_example"},
            "notes": f"seed from wizard calibration @ {entry.get('judged_at', '')}",
            # created_at feeds the D15 time-based golden split.
            "created_at": entry.get("judged_at") or "",
        }
        out = target / f"{slug}.yaml"
        # If slug collides (rare), suffix a number.
        i = 1
        while out.exists():
            out = target / f"{slug}-{i}.yaml"
            i += 1
        out.write_text(
            yaml.safe_dump(blob, sort_keys=False, allow_unicode=True,
                           default_flow_style=False),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Draft mutation helpers (unchanged shape from earlier sections)
# ---------------------------------------------------------------------------


def apply_profile_draft(draft: WizardV2Draft, profile_draft, *, is_regen: bool = False,
                        only_section: str = "") -> None:
    """Copy fields from a `pipeline.profile_draft.ProfileDraft` into the wizard
    draft. When `only_section` is set, only that section is overwritten (used
    by per-section regen). `is_regen` is metadata only — the caller updates
    the counter."""
    if profile_draft is None:
        return
    to_apply = {
        "description": profile_draft.description,
        "aliases": list(profile_draft.aliases),
        "not_to_be_confused_with": list(profile_draft.not_to_be_confused_with),
        "competitors": list(profile_draft.competitors),
        "scope_in": list(profile_draft.scope_in),
        "scope_out": list(profile_draft.scope_out),
        "suggested_sources": [
            {
                "plugin_id": s.plugin_id,
                "stream_config": dict(s.stream_config or {}),
                "rationale": s.rationale,
                "requires_key": bool(s.requires_key),
                # Start unchecked. Step 3's pick screen is where the user
                # explicitly opts in — the sub-wizard then walks them
                # through configuration for whichever ones they picked.
                "enabled": False,
            }
            for s in (profile_draft.suggested_sources or [])
        ],
    }
    if only_section:
        if only_section in to_apply:
            setattr(draft, only_section, to_apply[only_section])
        return
    for k, v in to_apply.items():
        setattr(draft, k, v)
