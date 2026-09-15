"""Source plugin contract (POST_V1_PLAN §4.1).

This module is the **public API** for source plugins. It's a stable contract
that both built-in sources (in `sources/*.py`) and third-party plugins
(drop-in `plugins/*.py` or pip-installed) implement.

To write a plugin:
1. Subclass `Source` with a `name: str` class attribute.
2. Implement `fetch_since(cursor, config, stats)` — yield `RawItem` objects.
3. At module level, declare a `MANIFEST = SourceManifest(...)`.

See [documents/PLUGIN_AUTHORS.md](../documents/PLUGIN_AUTHORS.md) for
the full author guide with worked examples.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Literal, Optional

from pipeline.models import RawItem


# ============================================================================
# Plugin manifest schema (ADR-0001)
# ============================================================================

MANIFEST_SCHEMA_VERSION = "1"


# The set of type strings that FieldSpec accepts. Kept as a frozenset so we
# can validate at manifest construction time.
_ALLOWED_FIELD_TYPES: frozenset[str] = frozenset({
    "text", "number", "bool", "csv", "textarea_list", "secret",
})

FieldType = Literal["text", "number", "bool", "csv", "textarea_list", "secret"]


# Source taxonomy — ADR-0021.
# source_category = which setup mechanism the plugin uses (drives UI grouping).
# content_types    = what kind of content the plugin surfaces (user comments vs
#                    published articles). A plugin can be tagged with one or both.
SourceCategory = Literal["rss_feed", "custom_source", "third_party_scraper"]
ContentType = Literal["user_feedback", "media_coverage"]

_ALLOWED_SOURCE_CATEGORIES: frozenset[str] = frozenset({
    "rss_feed", "custom_source", "third_party_scraper",
})
_ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset({
    "user_feedback", "media_coverage",
})


@dataclass
class FieldSpec:
    """Declares one input field for a source plugin.

    Used in two places on `SourceManifest`:
      - `connection_fields` — env vars needed at the /connections page
      - `stream_fields` — per-stream config on the product's Sources page

    Field types:
      text          — single-line input
      number        — integer or float, HTML type="number"
      bool          — checkbox
      csv           — comma-separated list; parsed into `list[str]`
      textarea_list — multi-line list, one item per line; parsed into `list[str]`
      secret        — treated like text but never rendered back in the UI (used
                      for API keys in .env)
    """

    name: str
    label: str
    type: FieldType = "text"
    required: bool = False
    default: Any = None
    help: str = ""
    placeholder: str = ""

    def __post_init__(self) -> None:
        if self.type not in _ALLOWED_FIELD_TYPES:
            raise ValueError(
                f"FieldSpec.type={self.type!r} must be one of {sorted(_ALLOWED_FIELD_TYPES)}"
            )


@dataclass
class SourceManifest:
    """Declarative metadata for a source plugin.

    Every source plugin exports a `MANIFEST = SourceManifest(...)` at module
    level next to its `Source` subclass. The webui reads all manifests at
    startup and builds its Connections + product Sources pages dynamically —
    no more hardcoded metadata dicts in webui/app.py.

    Field notes:
      plugin_id                  Unique key. Also serves as the sources.yaml
                                 `type:` value. Must be a valid Python
                                 identifier + underscores (no dots or slashes).
      display_name               Human-readable name shown in the UI.
      version                    Semver-ish, informational.
      category                   'source' (fetching pipeline items) or
                                 'assistant_llm' (a new global connection type
                                 that provides an LLM for wizard / snippet /
                                 prompt-suggestion work).
      manifest_schema_version    Bumped when this schema itself gains an
                                 incompatible change. Discovery code checks
                                 compatibility.
      connection_fields          .env vars shown on /connections/<plugin_id>.
                                 The `name` of each FieldSpec is the env var
                                 name.
      stream_fields              Per-stream config fields on the product's
                                 Sources form. The `name` of each FieldSpec
                                 is the YAML key under each stream.
      identifier_field           Which stream field is the "identifier" for
                                 the flat-table Identifier column
                                 (e.g. "subreddit" for reddit, "feed_url"
                                 for rss).
      credibility_weight_default Default per-instance credibility weight if
                                 the user doesn't set one.
      supports_bulk_add          If True, the Add Stream modal offers a
                                 textarea-one-per-line for the identifier
                                 field, expanding to N streams on save.
      supports_pause             If True, per-stream pause is offered in the
                                 UI. Rare to set False; there for exotic
                                 sources where pause makes no sense.
      source_category            ADR-0021 taxonomy — setup mechanism used by
                                 this plugin. Drives UI grouping.
                                   rss_feed              — feed URL, no auth
                                   custom_source         — site-specific auth
                                   third_party_scraper   — scraper API vendor
      content_types              What kind of content the plugin surfaces.
                                 One or both of "user_feedback" (Reddit posts,
                                 App Store reviews, GitHub issues) and
                                 "media_coverage" (news, blogs, announcements).
    """

    plugin_id: str
    display_name: str
    version: str = "0.0.1"
    category: Literal["source", "assistant_llm"] = "source"
    manifest_schema_version: str = MANIFEST_SCHEMA_VERSION

    docs_url: str = ""
    help: str = ""

    connection_fields: list[FieldSpec] = field(default_factory=list)
    stream_fields: list[FieldSpec] = field(default_factory=list)
    identifier_field: str = ""

    credibility_weight_default: float = 1.0
    supports_bulk_add: bool = False
    supports_pause: bool = True

    # ADR-0021 — source taxonomy fields. Default source_category=custom_source
    # keeps every pre-existing plugin working; content_types defaults to
    # ["user_feedback"] because that's the majority case (Reddit/HN/etc.).
    # Plugins should declare both explicitly rather than rely on the defaults.
    source_category: SourceCategory = "custom_source"
    content_types: list[ContentType] = field(default_factory=lambda: ["user_feedback"])

    def __post_init__(self) -> None:
        if not self.plugin_id:
            raise ValueError("SourceManifest.plugin_id must be non-empty")
        if not self.plugin_id.replace("_", "").isalnum():
            raise ValueError(
                f"SourceManifest.plugin_id={self.plugin_id!r} must contain only "
                f"letters, digits, and underscores"
            )
        if self.manifest_schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(
                f"SourceManifest.manifest_schema_version={self.manifest_schema_version!r} "
                f"is incompatible with this runtime's schema {MANIFEST_SCHEMA_VERSION!r}"
            )
        # identifier_field, if set, must reference an actual stream field
        if self.identifier_field:
            names = {f.name for f in self.stream_fields}
            if self.identifier_field not in names:
                raise ValueError(
                    f"SourceManifest.identifier_field={self.identifier_field!r} "
                    f"not in stream_fields; must be one of {sorted(names)}"
                )
        if self.category == "source":
            if self.source_category not in _ALLOWED_SOURCE_CATEGORIES:
                raise ValueError(
                    f"SourceManifest.source_category={self.source_category!r} "
                    f"must be one of {sorted(_ALLOWED_SOURCE_CATEGORIES)}"
                )
            bad = [c for c in self.content_types if c not in _ALLOWED_CONTENT_TYPES]
            if bad:
                raise ValueError(
                    f"SourceManifest.content_types has unknown values {bad}; "
                    f"must be from {sorted(_ALLOWED_CONTENT_TYPES)}"
                )
            if not self.content_types:
                raise ValueError(
                    f"SourceManifest.content_types cannot be empty for a source plugin"
                )


# ============================================================================
# Source ABC — the runtime contract
# ============================================================================


@dataclass
class SourceCursor:
    """Opaque per-stream cursor. For Reddit, cursor_ts is a UTC epoch second."""

    cursor_ts: Optional[float] = None


@dataclass
class FetchStats:
    """Completeness signals surfaced to runs.completeness (§11.4)."""

    fetched: int = 0
    ceiling_hits: list[tuple[str, float]] = None  # (stream, cursor_gap_seconds)
    comment_cap_hits: list[tuple[str, int, int]] = None  # (post_id, estimated, fetched)

    def __post_init__(self) -> None:
        if self.ceiling_hits is None:
            self.ceiling_hits = []
        if self.comment_cap_hits is None:
            self.comment_cap_hits = []


class Source(ABC):
    """Base class for a source plugin's runtime.

    Subclass this in your plugin module. Alongside the subclass, declare
    a `MANIFEST = SourceManifest(...)` at module level.

    Instance lifetime + thread-safety (ADR-0023):

      One Source instance is constructed per run and reused across every
      configured stream of that source type. When `fetch_concurrency_enabled`
      is true (default since 2026-09-15), `fetch_since` MAY be invoked
      CONCURRENTLY on the same instance from multiple threads — one per
      stream configured for this source, subject to per-host semaphores.

      Concrete implications for authors:

        - Keep per-call state LOCAL to `fetch_since` (locals, generators),
          not `self`. Storing a cursor, page counter, or rate-limit
          budget on `self` and mutating it from `fetch_since` will
          corrupt silently across concurrent invocations.
        - Anything shared on `self` (an httpx.Client, an SDK session,
          a token bucket) MUST be thread-safe. `httpx.Client` is safe.
          `praw.Reddit` is NOT — see sources/reddit.py for the
          instance-lock pattern. First-party audit table lives in
          ADR-0023.
        - The `SourceCursor` and `FetchStats` args are passed per-call
          and each belongs to exactly ONE stream — they are safe to
          mutate freely inside your `fetch_since`.

      The plugin conformance kit (ADR-0027) ships a threaded test that
      drives `fetch_since` from two threads and asserts disjoint results
      + monotonic cursor. Run it before shipping.
    """

    name: str

    @abstractmethod
    def fetch_since(
        self, cursor: SourceCursor, config: dict[str, Any], stats: FetchStats
    ) -> Iterator[RawItem]:
        """Yield items newer than cursor; update cursor as you go.

        MUST populate `RawItem.url` with a direct deep link to the original.

        MAY be invoked concurrently on one Source instance under
        `fetch_concurrency_enabled` (ADR-0023). Keep per-call state
        local; guard any shared mutable state on `self`.

        Args:
          cursor  Per-stream cursor. Mutate its `cursor_ts` as new items are
                  yielded so the next run can resume. This object is
                  local to one call — safe to mutate without a lock.
          config  The stream's config block from sources.yaml, merged with
                  global fetching defaults.
          stats   Completeness signals: append to `ceiling_hits` when a
                  provider's paging limit prevents fetching all new items;
                  append to `comment_cap_hits` when per-post comment caps
                  bit down a hot thread. Also local to one call.
        """
        raise NotImplementedError
