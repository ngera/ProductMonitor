"""ProductSpec loader (Phase 0 + admin-tool rename).

A `ProductSpec` carries everything that's per-product: prompts, taxonomy,
vendors, sources, llm routing, the extras Pydantic class, the composed
Classification schema, and labeled snippets. Loaded once per run from
`products/<product_id>/`.

Directory contract:

    products/<id>/
    ├── product.yaml           # display, description, extras_module, extras_class
    ├── sources.yaml           # source instances + streams
    ├── taxonomy.yaml          # areas + nested features (with descriptions)
    ├── vendors.yaml           # vendors + products
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


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"product config missing: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _version_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


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
    vendors: dict[str, Any]
    prompts: dict[str, Any]
    llm_routing: dict[str, Any]

    # Stable hashes for trend continuity (DESIGN.md §6.2)
    taxonomy_version: str
    vendors_version: str

    # Labeled snippets (positive + negative, holdout-flagged subset)
    snippets: list[Snippet] = field(default_factory=list)

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


@lru_cache(maxsize=8)
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
    vendors_blob = _load_yaml(product_dir / "vendors.yaml")
    prompts_blob = _load_yaml(product_dir / "prompts.yaml")
    llm_routing_blob = _load_yaml(product_dir / "llm_routing.yaml")

    extras_module = product_meta.get("extras_module", "extras")
    extras_class = product_meta.get("extras_class", "ProductExtras")
    extras_cls = _load_extras_class(product_dir, extras_module, extras_class)
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
        vendors=vendors_blob,
        prompts=prompts_blob,
        llm_routing=llm_routing_blob,
        taxonomy_version=str(
            taxonomy_blob.get("version") or _version_hash(product_dir / "taxonomy.yaml")
        ),
        vendors_version=str(
            vendors_blob.get("version") or _version_hash(product_dir / "vendors.yaml")
        ),
        snippets=snippets,
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

extras_module: extras
extras_class: ProductExtras

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

_SCAFFOLD_VENDORS_YAML = """\
# Vendor + product seed list for entity extraction. Used by the regex
# pre-pass and as hints to the classifier. Add the brands / hardware /
# software your product typically mentions.
version: "{today}"

vendors: []
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

    CONTENT TYPES (multi-select):
    {{content_types}}

    If you tag bug_report, fill bug_* including repro_steps extracted verbatim
    if present (else null and bug_repro_steps_quality="none").
    If you tag feature_request, fill request_*.
    {{extras_instructions}}

    For each entity assign:
      type (controlled vocab), vendor, product, version, role, confidence (0-1), verbatim.
      role: feature_implicated (user blames it) | hardware_in_use | software_in_use.

    {{few_shot_block}}
    REGEX PRE-PASS HINTS (confirm/correct, add what was missed, discard false positives):
      vendors: {{vendor_hits}}
      KB numbers: {{kb_numbers}}
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


def scaffold_product(product_id: str, display: str, description: str = "") -> Path:
    """Create products/<product_id>/ from in-tree templates.

    Used by the admin UI's "create product" form. Refuses to overwrite an
    existing product directory. Scaffolds with one mandatory feature under
    the seed `general` area so the "features required" rule holds from day
    one.
    """
    from datetime import date

    if not product_id or "/" in product_id or "\\" in product_id:
        raise ValueError(f"invalid product_id: {product_id!r}")
    target = PRODUCTS_DIR / product_id
    if target.exists():
        raise FileExistsError(f"product already exists at {target}")

    description = description.strip() or f"User feedback about {display}."
    today = date.today().isoformat()

    target.mkdir(parents=True)
    (target / "examples" / "positive").mkdir(parents=True)
    (target / "examples" / "negative").mkdir(parents=True)

    (target / "product.yaml").write_text(
        _SCAFFOLD_PRODUCT_YAML.format(id=product_id, display=display, description=description),
        encoding="utf-8",
    )
    (target / "extras.py").write_text(
        _SCAFFOLD_EXTRAS_PY.format(display=display),
        encoding="utf-8",
    )
    (target / "taxonomy.yaml").write_text(
        _SCAFFOLD_TAXONOMY_YAML.format(display=display, today=today),
        encoding="utf-8",
    )
    (target / "vendors.yaml").write_text(
        _SCAFFOLD_VENDORS_YAML.format(today=today),
        encoding="utf-8",
    )
    (target / "sources.yaml").write_text(
        _SCAFFOLD_SOURCES_YAML.format(id=product_id, display=display),
        encoding="utf-8",
    )
    (target / "prompts.yaml").write_text(
        _SCAFFOLD_PROMPTS_YAML.format(product_display=display),
        encoding="utf-8",
    )
    (target / "llm_routing.yaml").write_text(_SCAFFOLD_LLM_ROUTING_YAML, encoding="utf-8")

    clear_cache()
    return target


def save_product_meta(product_id: str, display: str, description: str, schedule: str = "weekly") -> None:
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
    existing.setdefault("extras_module", "extras")
    existing.setdefault("extras_class", "ProductExtras")
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
