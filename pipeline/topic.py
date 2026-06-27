"""TopicSpec loader (Phase 0).

A `TopicSpec` carries everything that's per-topic: prompts, taxonomy, vendors,
sources, llm routing, the extras Pydantic class, and the composed Classification
schema. Loaded once per run from `topics/<topic_id>/`.

Directory contract (see LOCAL_V1_PLAN.md §4 and DESIGN_SPECIFICATION.md §4.1):

    topics/<id>/
    ├── topic.yaml             # display, description, extras_module, extras_class
    ├── sources.yaml           # source instances + streams
    ├── taxonomy.yaml          # areas (+ version)
    ├── vendors.yaml           # vendors + products (+ version)
    ├── prompts.yaml           # relevance / classify prompt templates
    ├── llm_routing.yaml       # per-stage adapter config
    ├── extras.py              # per-topic Pydantic extension class
    └── examples/              # labeled snippets (Phase 5)
        ├── positive/*.yaml
        └── negative/*.yaml
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

TOPICS_DIR = Path(__file__).resolve().parent.parent / "topics"
DEFAULT_TOPIC = "windows"


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"topic config missing: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _version_hash(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _load_extras_class(topic_dir: Path, module_name: str, class_name: str) -> Type[BaseModel]:
    """Import topics/<id>/<module_name>.py and return its <class_name>."""
    py_path = topic_dir / f"{module_name}.py"
    if not py_path.exists():
        raise FileNotFoundError(
            f"extras module missing for topic {topic_dir.name}: {py_path}"
        )
    spec_name = f"feedback_monitor_topic_{topic_dir.name}_{module_name}"
    spec = importlib.util.spec_from_file_location(spec_name, py_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load extras module at {py_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec_name] = mod
    spec.loader.exec_module(mod)
    if not hasattr(mod, class_name):
        raise AttributeError(
            f"{py_path} has no class {class_name!r} (referenced by topic.yaml)"
        )
    cls = getattr(mod, class_name)
    if not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise TypeError(f"{class_name} must be a pydantic BaseModel subclass")
    return cls


@dataclass
class TopicSpec:
    """All per-topic state. Built once per run from topics/<id>/."""

    id: str
    display: str
    description: str
    dir: Path

    # Pydantic extension + composed schema
    extras_cls: Type[BaseModel]
    classification_schema: Type[CoreClassification]

    # Raw config blobs
    topic_meta: dict[str, Any]
    sources: list[dict[str, Any]]
    taxonomy: dict[str, Any]
    vendors: dict[str, Any]
    prompts: dict[str, Any]
    llm_routing: dict[str, Any]

    # Stable hashes for trend continuity (DESIGN.md §6.2)
    taxonomy_version: str
    vendors_version: str

    # Phase 5: labeled snippets (positive + negative, holdout-flagged subset)
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
def load_topic(topic_id: str = DEFAULT_TOPIC) -> TopicSpec:
    """Load and validate a topic. Cached per topic_id within a process."""
    topic_dir = TOPICS_DIR / topic_id
    if not topic_dir.is_dir():
        raise FileNotFoundError(
            f"topic '{topic_id}' not found at {topic_dir}. "
            f"Existing topics: {[p.name for p in TOPICS_DIR.iterdir() if p.is_dir()] if TOPICS_DIR.exists() else []}"
        )

    topic_meta = _load_yaml(topic_dir / "topic.yaml")
    sources_blob = _load_yaml(topic_dir / "sources.yaml")
    taxonomy_blob = _load_yaml(topic_dir / "taxonomy.yaml")
    vendors_blob = _load_yaml(topic_dir / "vendors.yaml")
    prompts_blob = _load_yaml(topic_dir / "prompts.yaml")
    llm_routing_blob = _load_yaml(topic_dir / "llm_routing.yaml")

    extras_module = topic_meta.get("extras_module", "extras")
    extras_class = topic_meta.get("extras_class", "Extras")
    extras_cls = _load_extras_class(topic_dir, extras_module, extras_class)
    classification_schema = build_classification_schema(extras_cls)

    snippets = load_snippets(topic_dir)

    return TopicSpec(
        id=topic_meta.get("id") or topic_id,
        display=topic_meta.get("display") or topic_id,
        description=topic_meta.get("description") or "",
        dir=topic_dir,
        extras_cls=extras_cls,
        classification_schema=classification_schema,
        topic_meta=topic_meta,
        sources=sources_blob.get("sources", []),
        taxonomy=taxonomy_blob,
        vendors=vendors_blob,
        prompts=prompts_blob,
        llm_routing=llm_routing_blob,
        taxonomy_version=str(
            taxonomy_blob.get("version") or _version_hash(topic_dir / "taxonomy.yaml")
        ),
        vendors_version=str(
            vendors_blob.get("version") or _version_hash(topic_dir / "vendors.yaml")
        ),
        snippets=snippets,
    )


def available_topics() -> list[str]:
    if not TOPICS_DIR.exists():
        return []
    return sorted([p.name for p in TOPICS_DIR.iterdir() if p.is_dir() and (p / "topic.yaml").exists()])


def clear_cache() -> None:
    """Drop the load_topic LRU cache. Useful after config edits via the UI."""
    load_topic.cache_clear()


# --- Scaffolding new topics --------------------------------------------------


_SCAFFOLD_TOPIC_YAML = """\
id: {id}
display: {display}
description: |
  {description}

extras_module: extras
extras_class: TopicExtras

schedule: weekly
"""

_SCAFFOLD_EXTRAS_PY = '''\
"""Per-topic extension fields for the {display} topic.

Composed onto CoreClassification at runtime. Add fields here for anything
this topic needs the LLM to fill that isn't already in CoreClassification.
Leave the class empty if you don't need any extras.
"""

from __future__ import annotations

from pydantic import BaseModel


class TopicExtras(BaseModel):
    pass
'''

_SCAFFOLD_TAXONOMY_YAML = """\
# Functional-area taxonomy for {display}. Edit to match your product.
# Each area needs an `id` (used in DB/joins) and a `display` label.
# `keywords` and `entity_type_hint` help the classifier and grouper.
# Bump `version` whenever you change the taxonomy so trend charts can mark
# a discontinuity.
version: "{today}"

areas:
  - id: general
    display: General
    enabled: true
    keywords: []
    entity_type_hint: []
"""

_SCAFFOLD_VENDORS_YAML = """\
# Vendor + product seed list for entity extraction. Used by the regex
# pre-pass and as hints to the classifier. Add the brands / hardware /
# software your topic typically mentions.
version: "{today}"

vendors: []
"""

_SCAFFOLD_SOURCES_YAML = """\
# Source instances for this topic. Each `id` is your label; each `type`
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
# LLM prompts for this topic. Edit via the UI or by hand.
# Placeholders for the relevance template:
#   {{topic_display}}  {{topic_description}}  {{title}}  {{body}}  {{few_shot_block}}

relevance:
  system: |
    You are a strict relevance classifier. Reply with JSON only.

  few_shot:
    enabled: true
    n_positive: 3
    n_negative: 2

  template: |
    Is this post about {{topic_display}}?

    {{few_shot_block}}
    Reply with a single JSON object: {{{{"relevant": true|false, "confidence": 0.0-1.0}}}}

    Title: {{title}}
    Body: {{body}}

classify:
  system: |
    You are classifying user feedback about {topic_display}.
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
# Per-stage LLM routing for this topic. Defaults to the same Foundry Local
# settings as the Windows reference topic; switch to Anthropic / OpenAI /
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


def scaffold_topic(topic_id: str, display: str, description: str = "") -> Path:
    """Create topics/<topic_id>/ from in-tree templates.

    Used by the admin UI's "create topic" form and by a CLI command. Refuses
    to overwrite an existing topic directory.
    """
    from datetime import date

    if not topic_id or "/" in topic_id or "\\" in topic_id:
        raise ValueError(f"invalid topic_id: {topic_id!r}")
    target = TOPICS_DIR / topic_id
    if target.exists():
        raise FileExistsError(f"topic already exists at {target}")

    description = description.strip() or f"User feedback about {display}."
    today = date.today().isoformat()

    target.mkdir(parents=True)
    (target / "examples" / "positive").mkdir(parents=True)
    (target / "examples" / "negative").mkdir(parents=True)

    (target / "topic.yaml").write_text(
        _SCAFFOLD_TOPIC_YAML.format(id=topic_id, display=display, description=description),
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
        _SCAFFOLD_SOURCES_YAML.format(id=topic_id, display=display),
        encoding="utf-8",
    )
    (target / "prompts.yaml").write_text(
        _SCAFFOLD_PROMPTS_YAML.format(topic_display=display),
        encoding="utf-8",
    )
    (target / "llm_routing.yaml").write_text(_SCAFFOLD_LLM_ROUTING_YAML, encoding="utf-8")

    clear_cache()
    return target
