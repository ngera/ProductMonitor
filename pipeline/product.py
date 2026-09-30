"""ProductSpec loader (Phase 0 + admin-tool rename).

A `ProductSpec` carries everything that's per-product: prompts, taxonomy,
sources, llm routing, the extras Pydantic class, the composed
Classification schema, and labeled snippets. Loaded once per run from
`products/<product_id>/`.

Directory contract:

    products/<id>/
    ├── product.yaml           # display, description, extras_module, extras_class
    ├── sources.yaml           # source instances + streams
    ├── taxonomy.yaml          # areas + nested features (with descriptions)
    ├── prompts.yaml           # relevance / classify prompt templates
    ├── llm_routing.yaml       # per-stage adapter config
    ├── extras.py              # per-product Pydantic extension class
    └── examples/              # labeled snippets (positive + negative)

(`topic` symbols are preserved as back-compat aliases for `product` at module
level so external callers that still import the old names keep working.)
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Type

import yaml
from pydantic import BaseModel

from pipeline.models import CoreClassification, build_classification_schema
from pipeline.snippets import Snippet, load_snippets

PRODUCTS_DIR = Path(__file__).resolve().parent.parent / "products"
DEFAULT_PRODUCT = "windows"


# --- Product facts (wizard redesign Phase 1) ---------------------------------
#
# Additive, optional fields on product.yaml so existing products load unchanged.
# The wizard v2 collects these directly; they also feed the relevance/classify
# prompts (aliases + confusables + scope bullets) and default source queries.

VALID_GOALS = (
    "bugs",
    "feature_requests",
    "sentiment",
    "competitor_compare",
    "churn_signals",
)

# Cap on list-shaped facts fields. Beyond this we refuse the save rather than
# silently truncating — long lists usually mean the user pasted something they
# meant to be prose.
MAX_FACTS_LIST_LEN = 20


def _clean_str_list(raw: Any) -> list[str]:
    """Coerce a scalar/list to a trimmed list[str], preserving order, deduped."""
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [raw]
    else:
        try:
            items = list(raw)
        except TypeError:
            return []
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        if it is None:
            continue
        s = str(it).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


# Palette used when a rich competitor object doesn't pin an explicit color.
# Matches the interim palette in pipeline/digest/charts.py so old plain-string
# entries keep rendering with the same colors they used to.
_COMPETITOR_DEFAULT_PALETTE = ["#a2a2a2", "#4285f4", "#137333", "#b06000"]


def _clean_competitor_list(raw: Any) -> list[dict[str, Any]]:
    """Normalize competitors to the rich `{name, aliases, color}` shape.

    Accepts either legacy plain strings (auto-lifted to `{name, aliases: [],
    color: None}`) or dicts. Dedupes on lowercased name, preserves order.
    Fields:
      - name:    trimmed non-empty string. Entries without a name are dropped.
      - aliases: list[str], each trimmed non-empty, deduped case-insensitively.
      - color:   optional CSS-color string; None means "use palette default".

    See report_v2_design.md §7.2 for the rationale.
    """
    if raw is None:
        return []
    if isinstance(raw, (str, dict)):
        items: list[Any] = [raw]
    else:
        try:
            items = list(raw)
        except TypeError:
            return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for it in items:
        if it is None:
            continue
        if isinstance(it, str):
            name, aliases, color, context = it.strip(), [], None, ""
        elif isinstance(it, dict):
            name = str(it.get("name") or "").strip()
            aliases = _clean_str_list(it.get("aliases"))
            color_raw = it.get("color")
            color = str(color_raw).strip() if color_raw else None
            context = str(it.get("context") or "").strip()
        else:
            name, aliases, color, context = str(it).strip(), [], None, ""
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        # Drop the name itself out of the aliases list if it snuck in — no
        # point matching the same string twice.
        aliases = [a for a in aliases if a.lower() != key]
        out.append({
            "name": name, "aliases": aliases,
            "color": color, "context": context,
        })
    return out


def competitor_color(competitor: dict[str, Any], index: int) -> str:
    """Effective color for a competitor — explicit if set, else palette."""
    c = (competitor or {}).get("color")
    if c:
        return c
    return _COMPETITOR_DEFAULT_PALETTE[index % len(_COMPETITOR_DEFAULT_PALETTE)]


def competitor_display_name(competitor: Any) -> str:
    """Extract the human-facing name from a rich competitor object OR a
    plain-string legacy entry. Used by prompt summarizers and templates
    that shouldn't care about the shape."""
    if isinstance(competitor, dict):
        return str(competitor.get("name") or "").strip()
    return str(competitor or "").strip()


def validate_facts(
    *,
    url: str = "",
    aliases: Any = None,
    not_to_be_confused_with: Any = None,
    goals: Any = None,
    competitors: Any = None,
    scope_in: Any = None,
    scope_out: Any = None,
) -> dict[str, Any]:
    """Normalize + validate the product-facts inputs. Raises ValueError on any
    per-field problem. Returns a dict of the cleaned values."""
    cleaned: dict[str, Any] = {
        "url": (url or "").strip(),
        "aliases": _clean_str_list(aliases),
        "not_to_be_confused_with": _clean_str_list(not_to_be_confused_with),
        "goals": _clean_str_list(goals),
        "competitors": _clean_competitor_list(competitors),
        "scope_in": _clean_str_list(scope_in),
        "scope_out": _clean_str_list(scope_out),
    }
    bad_goals = [g for g in cleaned["goals"] if g not in VALID_GOALS]
    if bad_goals:
        raise ValueError(
            f"invalid goals: {bad_goals}. valid: {list(VALID_GOALS)}"
        )
    for k in ("aliases", "not_to_be_confused_with", "goals", "competitors",
              "scope_in", "scope_out"):
        if len(cleaned[k]) > MAX_FACTS_LIST_LEN:
            raise ValueError(
                f"{k} has {len(cleaned[k])} entries, cap is {MAX_FACTS_LIST_LEN}"
            )
    return cleaned


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"product config missing: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _version_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


@lru_cache(maxsize=1)
def _empty_product_extras() -> Type[BaseModel]:
    """Return a shared empty ProductExtras class for products that don't ship
    their own extras.py. Cached so `build_classification_schema` sees the
    same class every time (Pydantic composes are keyed by identity)."""
    class ProductExtras(BaseModel):
        pass
    return ProductExtras


def _load_extras_class(product_dir: Path, module_name: str, class_name: str) -> Type[BaseModel]:
    """Import products/<id>/<module_name>.py and return its <class_name>."""
    py_path = product_dir / f"{module_name}.py"
    if not py_path.exists():
        raise FileNotFoundError(
            f"extras module missing for product {product_dir.name}: {py_path}"
        )
    spec_name = f"feedback_monitor_product_{product_dir.name}_{module_name}"
    spec = importlib.util.spec_from_file_location(spec_name, py_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load extras module at {py_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec_name] = mod
    spec.loader.exec_module(mod)
    if not hasattr(mod, class_name):
        # Back-compat: scaffold templates used to emit `TopicExtras`; accept
        # that name if `ProductExtras` isn't present.
        if class_name == "ProductExtras" and hasattr(mod, "TopicExtras"):
            cls = getattr(mod, "TopicExtras")
        else:
            raise AttributeError(
                f"{py_path} has no class {class_name!r} (referenced by product.yaml)"
            )
    else:
        cls = getattr(mod, class_name)
    if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise TypeError(f"{class_name} must be a pydantic BaseModel subclass")
    return cls


@dataclass
class ProductSpec:
    """All per-product state. Built once per run from products/<id>/."""

    id: str
    display: str
    description: str
    dir: Path

    # Pydantic extension + composed schema
    extras_cls: Type[BaseModel]
    classification_schema: Type[CoreClassification]

    # Raw config blobs
    product_meta: dict[str, Any]
    sources: list[dict[str, Any]]
    taxonomy: dict[str, Any]
    prompts: dict[str, Any]
    llm_routing: dict[str, Any]

    # Stable hashes for trend continuity (DESIGN.md §6.2)
    taxonomy_version: str

    # Persisted time-range setting for runs. CLI flags can override per-run.
    # Shape: {mode: 'incremental'|'last_week'|'last_month'|'range',
    #         range_from: 'YYYY-MM-DD' or None,
    #         range_to:   'YYYY-MM-DD' or None}
    time_range: dict[str, Any] = field(default_factory=lambda: {"mode": "incremental"})

    # Labeled snippets (positive + negative, holdout-flagged subset)
    snippets: list[Snippet] = field(default_factory=list)

    # --- Product-facts fields (wizard redesign Phase 1) ---------------------
    # All optional; empty defaults keep legacy product.yaml files loading
    # unchanged. Consumed by the relevance/classify prompts and by wizard v2.
    url: str = ""
    aliases: list[str] = field(default_factory=list)
    not_to_be_confused_with: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    # Rich objects per report_v2_design.md §7.2 — {name, aliases, color}.
    # Legacy plain-string entries in product.yaml are coerced at load time
    # by _clean_competitor_list, so existing products continue to load.
    competitors: list[dict[str, Any]] = field(default_factory=list)
    scope_in: list[str] = field(default_factory=list)
    scope_out: list[str] = field(default_factory=list)

    # Cached helpers
    _area_ids: Optional[list[str]] = field(default=None, repr=False)
    _entity_type_to_area_map: Optional[dict[str, str]] = field(default=None, repr=False)

    # ----- helpers ----------------------------------------------------------

    def enabled_areas(self) -> list[dict[str, Any]]:
        return [a for a in (self.taxonomy.get("areas") or []) if a.get("enabled", True)]

    def area_ids(self) -> list[str]:
        if self._area_ids is None:
            self._area_ids = [a["id"] for a in self.enabled_areas()]
        return self._area_ids

    def features_for(self, area_id: str) -> list[dict[str, Any]]:
        """Return the list of features declared under a given area."""
        for a in self.enabled_areas():
            if a.get("id") == area_id:
                return list(a.get("features") or [])
        return []

    def all_features(self) -> list[dict[str, Any]]:
        """Flat list of every feature across all areas. Each item is a copy of
        the feature dict with an added `area_id` key for grouping in the UI
        and in the classify prompt."""
        out: list[dict[str, Any]] = []
        for area in self.enabled_areas():
            for feat in (area.get("features") or []):
                merged = {**feat, "area_id": area["id"], "area_display": area.get("display", area["id"])}
                out.append(merged)
        return out

    def entity_type_to_area(self) -> dict[str, str]:
        """Map entity `type` -> area id, from taxonomy entity_type_hint (§4.8.1).

        First-declared area wins on conflict (stable, config-driven).
        """
        if self._entity_type_to_area_map is None:
            mapping: dict[str, str] = {}
            for area in self.enabled_areas():
                for t in area.get("entity_type_hint", []) or []:
                    mapping.setdefault(t, area["id"])
            self._entity_type_to_area_map = mapping
        return self._entity_type_to_area_map


@lru_cache(maxsize=64)
def load_product(product_id: str = DEFAULT_PRODUCT) -> ProductSpec:
    """Load and validate a product. Cached per product_id within a process."""
    product_dir = PRODUCTS_DIR / product_id
    if not product_dir.is_dir():
        raise FileNotFoundError(
            f"product '{product_id}' not found at {product_dir}. "
            f"Existing products: {[p.name for p in PRODUCTS_DIR.iterdir() if p.is_dir()] if PRODUCTS_DIR.exists() else []}"
        )

    # Back-compat: accept product.yaml (new) or topic.yaml (old) until users migrate.
    meta_path = product_dir / "product.yaml"
    if not meta_path.exists():
        legacy = product_dir / "topic.yaml"
        if legacy.exists():
            meta_path = legacy

    product_meta = _load_yaml(meta_path)
    sources_blob = _load_yaml(product_dir / "sources.yaml")
    taxonomy_blob = _load_yaml(product_dir / "taxonomy.yaml")
    prompts_blob = _load_yaml(product_dir / "prompts.yaml")
    llm_routing_blob = _load_yaml(product_dir / "llm_routing.yaml")

    # Extras are optional as of wizard v2 (see ADR-0015). If the product.yaml
    # doesn't declare extras or the file is missing, we substitute an empty
    # ProductExtras. Products created by the v1 wizard (or by hand) that DO
    # declare extras keep loading the file exactly as before.
    extras_module = product_meta.get("extras_module")
    extras_class = product_meta.get("extras_class")
    if extras_module and (product_dir / f"{extras_module}.py").exists():
        extras_cls = _load_extras_class(
            product_dir, extras_module, extras_class or "ProductExtras",
        )
    else:
        extras_cls = _empty_product_extras()
    classification_schema = build_classification_schema(extras_cls)

    # Validate the hierarchy: every enabled area must declare >= 1 feature.
    # Required by user direction; enforced here so saves through the UI or by
    # hand both fail loud.
    enabled_areas_blob = [a for a in (taxonomy_blob.get("areas") or []) if a.get("enabled", True)]
    bad_areas = [a.get("id") for a in enabled_areas_blob if not (a.get("features") or [])]
    if bad_areas:
        raise ValueError(
            f"product '{product_id}' taxonomy.yaml: every enabled area must have "
            f"at least one feature. Areas missing features: {bad_areas}"
        )

    snippets = load_snippets(product_dir)

    # Product-facts fields — cleaned, but don't fail load on invalid values;
    # legacy files may pre-date validation. Log-and-drop happens implicitly
    # via _clean_str_list (goals get filtered against VALID_GOALS below).
    facts_goals = [
        g for g in _clean_str_list(product_meta.get("goals")) if g in VALID_GOALS
    ]

    return ProductSpec(
        id=product_meta.get("id") or product_id,
        display=product_meta.get("display") or product_id,
        description=product_meta.get("description") or "",
        dir=product_dir,
        extras_cls=extras_cls,
        classification_schema=classification_schema,
        product_meta=product_meta,
        sources=sources_blob.get("sources", []),
        taxonomy=taxonomy_blob,
        prompts=prompts_blob,
        llm_routing=llm_routing_blob,
        taxonomy_version=str(
            taxonomy_blob.get("version") or _version_hash(product_dir / "taxonomy.yaml")
        ),
        snippets=snippets,
        time_range=product_meta.get("time_range") or {"mode": "incremental"},
        url=(product_meta.get("url") or "").strip(),
        aliases=_clean_str_list(product_meta.get("aliases")),
        not_to_be_confused_with=_clean_str_list(
            product_meta.get("not_to_be_confused_with")
        ),
        goals=facts_goals,
        competitors=_clean_competitor_list(product_meta.get("competitors")),
        scope_in=_clean_str_list(product_meta.get("scope_in")),
        scope_out=_clean_str_list(product_meta.get("scope_out")),
    )


def available_products() -> list[str]:
    if not PRODUCTS_DIR.exists():
        return []
    return sorted([
        p.name for p in PRODUCTS_DIR.iterdir()
        if p.is_dir() and ((p / "product.yaml").exists() or (p / "topic.yaml").exists())
    ])


def clear_cache() -> None:
    """Drop the load_product LRU cache. Useful after config edits via the UI."""
    load_product.cache_clear()


# --- Scaffolding new products ------------------------------------------------


_SCAFFOLD_PRODUCT_YAML = """\
id: {id}
display: {display}
description: |
  {description}

schedule: weekly
"""

_SCAFFOLD_EXTRAS_PY = '''\
"""Per-product extension fields for the {display} product.

Composed onto CoreClassification at runtime. Add fields here for anything
this product needs the LLM to fill that isn't already in CoreClassification.
Leave the class empty if you don't need any extras.
"""

from __future__ import annotations

from pydantic import BaseModel


class ProductExtras(BaseModel):
    pass
'''

_SCAFFOLD_TAXONOMY_YAML = """\
# Functional-area taxonomy for {display}. Edit to match your product.
# Each area MUST have at least one feature. The feature's `description`
# is the prompt the LLM uses to recognise content about that feature.
# Bump `version` whenever you change the taxonomy so trend charts can mark
# a discontinuity.
version: "{today}"

areas:
  - id: general
    display: General
    enabled: true
    keywords: []
    entity_type_hint: []
    features:
      - id: general
        display: General feedback
        description: |
          Any public discussion about {display} that doesn't fit a more
          specific area. Edit this default and add more features as you
          curate the taxonomy.
"""

_SCAFFOLD_SOURCES_YAML = """\
# Source instances for this product. Each `id` is your label; each `type`
# matches a registered source plugin (see sources/__init__.py).
#
# Hacker News is included as a starter because it needs no auth.
# Edit / add sources via the UI or by hand.
sources:
  - id: hn
    type: hn
    credibility_weight: 1.0
    streams:
      - name: hn-{id}
        search_queries:
          - "{display}"
        include_tags: [story]
        max_pages_per_query: 3
        hits_per_page: 50
"""

_SCAFFOLD_PROMPTS_YAML = """\
# LLM prompts for this product. Edit via the UI or by hand.
# Placeholders for the relevance template:
#   {{product_display}}  {{product_description}}  {{title}}  {{body}}  {{few_shot_block}}

relevance:
  system: |
    You are a strict relevance classifier. Reply with JSON only.

  few_shot:
    enabled: true
    n_positive: 3
    n_negative: 2

  template: |
    Is this post about {{product_display}}?

    {{few_shot_block}}
    Reply with a single JSON object: {{{{"relevant": true|false, "confidence": 0.0-1.0}}}}

    Title: {{title}}
    Body: {{body}}

classify:
  system: |
    You are classifying user feedback about {product_display}.
    Return only JSON matching the requested schema. Use multi-label where
    applicable. Use "unknown" or null rather than guessing.

  extras_instructions: ""

  few_shot:
    enabled: true
    n_positive: 2
    n_negative: 1

  template: |
    Read the post and return JSON matching the schema.

    ENABLED AREAS (multi-select; use the id):
    {{areas}}

    FEATURES (within each area, specific things to look for — use these
    descriptions to decide which areas to tag):
    {{features}}

    CONTENT TYPES (multi-select):
    {{content_types}}

    If you tag bug_report, fill bug_* including repro_steps extracted verbatim
    if present (else null and bug_repro_steps_quality="none").
    If you tag feature_request, fill request_*.
    {{extras_instructions}}

    For each entity assign:
      type (controlled vocab), product, version, role, confidence (0-1), verbatim.
      role: feature_implicated (user blames it) | hardware_in_use | software_in_use.

    {{few_shot_block}}
    REGEX PRE-PASS HINTS (confirm/correct, add what was missed, discard false positives):
      build numbers: {{build_numbers}}
    {{parent_block}}
    POST:
    TITLE: {{title}}
    BODY: {{body}}
    ENGAGEMENT: {{engagement}}
    SOURCE: {{source}}

    Return ONLY valid JSON.
"""

_SCAFFOLD_LLM_ROUTING_YAML = """\
# Per-stage LLM routing for this product. Defaults to the same Foundry Local
# settings as the Windows reference product; switch to Anthropic / OpenAI /
# Ollama / etc. as you prefer.

relevance:
  endpoint: http://localhost:5273/v1
  model: phi-4-mini
  temperature: 0
  seed: 42
  timeout_seconds: 20
  max_retries: 3

classify:
  endpoint: http://localhost:5273/v1
  model: phi-4-mini
  temperature: 0
  seed: 42
  timeout_seconds: 60
  max_retries: 3
  use_guided_decoding: true
  fallback_repair_attempts: 1
"""


def scaffold_product(
    product_id: str,
    display: str,
    description: str = "",
    facts: Optional[dict[str, Any]] = None,
) -> Path:
    """Create products/<product_id>/ from in-tree templates.

    Used by the admin UI's "create product" form. Refuses to overwrite an
    existing product directory. Scaffolds with one mandatory feature under
    the seed `general` area so the "features required" rule holds from day
    one.

    `facts`, when provided, is validated via `validate_facts` and merged
    into product.yaml so wizard v2 can pass everything it collected in one
    call.
    """
    from datetime import date

    if not product_id or "/" in product_id or "\\" in product_id:
        raise ValueError(f"invalid product_id: {product_id!r}")
    target = PRODUCTS_DIR / product_id
    if target.exists():
        raise FileExistsError(f"product already exists at {target}")

    description = description.strip() or f"User feedback about {display}."
    today = date.today().isoformat()

    cleaned_facts = validate_facts(**(facts or {}))

    target.mkdir(parents=True)
    (target / "examples" / "positive").mkdir(parents=True)
    (target / "examples" / "negative").mkdir(parents=True)

    product_yaml = _SCAFFOLD_PRODUCT_YAML.format(
        id=product_id, display=display, description=description
    )
    # Append optional facts fields when any are set — keeps the scaffold minimal
    # when the caller didn't supply them.
    if any(cleaned_facts.get(k) for k in cleaned_facts):
        facts_yaml = yaml.safe_dump(
            cleaned_facts, sort_keys=False, allow_unicode=True,
            default_flow_style=False,
        )
        product_yaml = product_yaml.rstrip() + "\n\n# Product facts (wizard v2)\n" + facts_yaml
    (target / "product.yaml").write_text(product_yaml, encoding="utf-8")
    # `extras.py` is no longer scaffolded by default (ADR-0015). Users who
    # need per-product custom classification fields add it via the Advanced
    # page. `load_product` substitutes an empty ProductExtras when absent.
    (target / "taxonomy.yaml").write_text(
        _SCAFFOLD_TAXONOMY_YAML.format(display=display, today=today),
        encoding="utf-8",
    )
    # If aliases were supplied via facts, include them in the default HN
    # stream's search_queries so the first fetch casts a wider net. Keep the
    # template as the fallback for the alias-less case (its literal form is
    # easier to hand-edit than a yaml.safe_dump round-trip).
    if cleaned_facts.get("aliases"):
        sources_yaml_text = yaml.safe_dump(
            {
                "sources": [
                    {
                        "id": "hn",
                        "type": "hn",
                        "credibility_weight": 1.0,
                        "streams": [
                            {
                                "name": f"hn-{product_id}",
                                "search_queries": [display, *cleaned_facts["aliases"]],
                                "include_tags": ["story"],
                                "max_pages_per_query": 3,
                                "hits_per_page": 50,
                            }
                        ],
                    }
                ]
            },
            sort_keys=False, allow_unicode=True, default_flow_style=False,
        )
    else:
        sources_yaml_text = _SCAFFOLD_SOURCES_YAML.format(id=product_id, display=display)
    (target / "sources.yaml").write_text(sources_yaml_text, encoding="utf-8")
    # Prompt scaffold uses editable templates from Admin > Prompts. The
    # relevance/classify system messages get `{product_display}` interpolated
    # so a fresh product's prompts.yaml carries the product name inline
    # (existing products aren't touched — this only affects future scaffolds).
    from pipeline import prompt_templates
    prompts_blob = {
        "relevance": {
            "system": prompt_templates.get("scaffold_relevance_system").replace(
                "{product_display}", display,
            ),
            "template": prompt_templates.get("scaffold_relevance_template"),
            "few_shot": {"enabled": True, "n_positive": 3, "n_negative": 2},
        },
        "classify": {
            "system": prompt_templates.get("scaffold_classify_system").replace(
                "{product_display}", display,
            ),
            "template": prompt_templates.get("scaffold_classify_template"),
            "extras_instructions": "",
            "few_shot": {"enabled": True, "n_positive": 2, "n_negative": 1},
        },
    }
    (target / "prompts.yaml").write_text(
        yaml.safe_dump(prompts_blob, sort_keys=False, allow_unicode=True,
                       default_flow_style=False),
        encoding="utf-8",
    )
    (target / "llm_routing.yaml").write_text(_SCAFFOLD_LLM_ROUTING_YAML, encoding="utf-8")

    clear_cache()
    return target


def save_product_facts(product_id: str, facts: dict[str, Any]) -> None:
    """Update the facts-shaped fields on products/<id>/product.yaml in place.

    Preserves all other keys (id, display, description, schedule, extras_*,
    time_range). Validates via `validate_facts`; raises ValueError on any
    per-field problem. Empty lists overwrite (i.e. this is a full save of
    the facts block, not a merge).
    """
    product_dir = PRODUCTS_DIR / product_id
    if not product_dir.is_dir():
        raise FileNotFoundError(f"product '{product_id}' not found")
    meta_path = product_dir / "product.yaml"
    if not meta_path.exists() and (product_dir / "topic.yaml").exists():
        (product_dir / "topic.yaml").rename(meta_path)
    existing: dict[str, Any] = _load_yaml(meta_path) if meta_path.exists() else {}
    cleaned = validate_facts(**facts)
    existing.update(cleaned)
    meta_path.write_text(
        yaml.safe_dump(existing, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    clear_cache()


def save_product_meta(
    product_id: str,
    display: str,
    description: str,
    schedule: str = "weekly",
    time_range: Optional[dict[str, Any]] = None,
) -> None:
    """Update products/<id>/product.yaml in place, preserving extras_module / class."""
    product_dir = PRODUCTS_DIR / product_id
    if not product_dir.is_dir():
        raise FileNotFoundError(f"product '{product_id}' not found")

    meta_path = product_dir / "product.yaml"
    if not meta_path.exists() and (product_dir / "topic.yaml").exists():
        # One-time migration: rename legacy file.
        (product_dir / "topic.yaml").rename(meta_path)

    existing: dict[str, Any] = {}
    if meta_path.exists():
        existing = _load_yaml(meta_path)
    existing.update({
        "id": product_id,
        "display": display.strip(),
        "description": description.strip(),
        "schedule": schedule.strip() or "weekly",
    })
    # Only preserve extras_* keys that the product already declared. We no
    # longer auto-insert them for products that never had an extras.py.
    if time_range is not None:
        # Strip empty range_from / range_to keys so non-range modes stay clean.
        tr = {"mode": time_range.get("mode") or "incremental"}
        if tr["mode"] == "range":
            tr["range_from"] = time_range.get("range_from") or None
            tr["range_to"] = time_range.get("range_to") or None
        existing["time_range"] = tr
    meta_path.write_text(
        yaml.safe_dump(existing, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    clear_cache()


# --- Back-compat aliases (so older `from pipeline.topic import ...` imports
#     keep working until we rename test/scripts/eval/UI imports below) -------

TopicSpec = ProductSpec
TOPICS_DIR = PRODUCTS_DIR
DEFAULT_TOPIC = DEFAULT_PRODUCT
load_topic = load_product
available_topics = available_products
scaffold_topic = scaffold_product
