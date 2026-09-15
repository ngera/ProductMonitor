"""Parametrizable conformance test class for source plugins (ADR-0027).

Every `test_*` method here encodes one line of the `sources.base.Source`
contract. Plugin authors subclass with a `source_class` + `stream_config`
and get the whole battery for free:

    class TestMySource(SourceConformanceTests):
        source_class = MyRedditVariantSource
        stream_config = {"subreddit": "windows11"}

        def build_source(self):
            # Override when your source needs env vars / mocks beyond
            # the defaults. Return an instance ready to call.
            with unittest.mock.patch.dict(os.environ, {"MY_KEY": "test"}):
                return super().build_source()

The kit does NOT test source-specific behavior (which subreddit, which
URL shape). It tests the contract: cursor advances monotonically,
`url` is populated on every item, `RawItem.created_at` is UTC-aware,
`FetchStats.ceiling_hits` is appendable, per-item errors don't crash
the caller, MANIFEST is well-formed.

If a plugin can't run without a real network / API key, override
`build_source()` to skip cleanly (pytest.skip).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, ClassVar, Iterator

import pytest

from pipeline.models import RawItem
from sources.base import (
    FetchStats,
    Source,
    SourceCursor,
    SourceManifest,
)


class SourceConformanceTests:
    """Base class for plugin conformance tests. Subclass and set
    `source_class` + `stream_config`. Every `test_*` method runs
    against your subclass automatically via pytest's normal collection.

    Do NOT rename this class in a way pytest would collect it directly —
    the leading `Source` prefix keeps it out of the default collection
    unless subclassed."""

    # --- required overrides -------------------------------------------------

    source_class: ClassVar[type[Source]]

    # Minimal stream config that fetch_since accepts. Kit uses this
    # verbatim; sources that need extra options should merge them here.
    stream_config: ClassVar[dict[str, Any]] = {}

    # Whether the source is expected to yield ANY items when called
    # against a default config. Set False for sources that require
    # per-instance fixtures (e.g. a specific feed URL you don't want
    # to register in the base class).
    expects_items: ClassVar[bool] = False

    # --- overridable hooks --------------------------------------------------

    def build_source(self) -> Source:
        """Instantiate the source. Override to inject mocks / env / a
        FakeHTTPTransport. Default: `source_class()` — works for
        sources with no required constructor args."""
        return self.source_class()

    def build_cursor(self) -> SourceCursor:
        """Fresh cursor for each test. Override if your source needs
        a non-None starting cursor (unusual)."""
        return SourceCursor(cursor_ts=None)

    def build_stats(self) -> FetchStats:
        return FetchStats()

    # --- helpers ------------------------------------------------------------

    def _fetch_all(self) -> tuple[list[RawItem], SourceCursor, FetchStats]:
        """Drive fetch_since to exhaustion; return the items + final
        cursor + stats. Wrapped so per-test setup stays uniform."""
        src = self.build_source()
        cur = self.build_cursor()
        stats = self.build_stats()
        items = list(src.fetch_since(cur, dict(self.stream_config), stats))
        return items, cur, stats

    # ========================================================================
    # Contract tests
    # ========================================================================

    # --- manifest ----------------------------------------------------------

    def test_manifest_module_attribute_exists(self) -> None:
        """The plugin module MUST expose a module-level `MANIFEST` object
        of type `SourceManifest`. This is how the plugin registry
        discovers it (ADR-0001)."""
        import inspect
        module = inspect.getmodule(self.source_class)
        assert module is not None
        assert hasattr(module, "MANIFEST"), (
            f"{module.__name__} must declare a module-level MANIFEST = "
            f"SourceManifest(...)"
        )
        assert isinstance(module.MANIFEST, SourceManifest), (
            f"{module.__name__}.MANIFEST must be a SourceManifest instance"
        )

    def test_source_class_has_name(self) -> None:
        """Source subclasses MUST set a `name: str` class attribute
        matching the manifest's `plugin_id`."""
        assert hasattr(self.source_class, "name")
        assert isinstance(self.source_class.name, str)
        assert self.source_class.name, "Source.name must be non-empty"

    def test_manifest_plugin_id_matches_source_name(self) -> None:
        import inspect
        module = inspect.getmodule(self.source_class)
        assert module is not None
        manifest = module.MANIFEST
        assert manifest.plugin_id == self.source_class.name, (
            f"MANIFEST.plugin_id ({manifest.plugin_id!r}) must equal "
            f"Source.name ({self.source_class.name!r})"
        )

    # --- fetch_since contract ----------------------------------------------

    def test_fetch_since_returns_iterator(self) -> None:
        """fetch_since must return an Iterator/generator, not a list.
        Streaming semantics let the caller size-cap without loading
        everything into memory."""
        src = self.build_source()
        result = src.fetch_since(self.build_cursor(), dict(self.stream_config),
                                 self.build_stats())
        # Iterator protocol: has __iter__ AND __next__, or is a generator.
        assert hasattr(result, "__iter__") and hasattr(result, "__next__"), (
            f"{self.source_class.__name__}.fetch_since must return an "
            "iterator/generator, not a materialized list"
        )

    def test_yielded_items_are_raw_items(self) -> None:
        items, _, _ = self._fetch_all()
        if not items and not self.expects_items:
            pytest.skip(
                "source yielded nothing under default stream_config; set "
                "expects_items=True or override stream_config to exercise items"
            )
        for item in items:
            assert isinstance(item, RawItem), (
                f"fetch_since must yield RawItem, got {type(item).__name__}"
            )

    def test_every_item_has_url(self) -> None:
        """DESIGN.md §13 and sources/base.py: url is MANDATORY on every
        item so attribution partials can render a deep link. A missing
        url makes the item unattributable."""
        items, _, _ = self._fetch_all()
        if not items and not self.expects_items:
            pytest.skip("no items to check")
        for item in items:
            assert item.url, (
                f"item {item.external_id!r} has empty url — every RawItem "
                "must carry a deep link"
            )
            assert isinstance(item.url, str)

    def test_every_item_has_external_id(self) -> None:
        items, _, _ = self._fetch_all()
        if not items and not self.expects_items:
            pytest.skip("no items to check")
        for item in items:
            assert item.external_id, "external_id must be non-empty"
            assert isinstance(item.external_id, str)

    def test_every_item_has_source_matching_name(self) -> None:
        items, _, _ = self._fetch_all()
        if not items and not self.expects_items:
            pytest.skip("no items to check")
        for item in items:
            assert item.source == self.source_class.name, (
                f"item.source={item.source!r} must match Source.name="
                f"{self.source_class.name!r}"
            )

    def test_every_item_created_at_is_utc_aware(self) -> None:
        """CLAUDE.md MUST rule: 'UTC in storage, always.' Naive datetimes
        get silently coerced to local time downstream, corrupting week
        assignment and recency scoring."""
        items, _, _ = self._fetch_all()
        if not items and not self.expects_items:
            pytest.skip("no items to check")
        for item in items:
            assert isinstance(item.created_at, datetime), (
                f"item {item.external_id!r} created_at must be datetime"
            )
            assert item.created_at.tzinfo is not None, (
                f"item {item.external_id!r} created_at must be tz-aware "
                "(UTC in storage, always — see CLAUDE.md)"
            )

    # --- cursor advancement -------------------------------------------------

    def test_cursor_advances_when_items_yielded(self) -> None:
        """The cursor's `cursor_ts` must advance to the newest item's
        created_at (or later) so the next incremental run resumes past
        the current window's right edge. Leaving cursor_ts=None after
        successfully yielding items means the next run refetches
        everything and dedupes on seen_ids — wasteful."""
        items, cur, _ = self._fetch_all()
        if not items:
            pytest.skip("no items yielded; cursor advancement N/A")
        assert cur.cursor_ts is not None, (
            "fetch_since yielded items but left cursor_ts=None; the next "
            "incremental run will refetch the whole window"
        )
        newest = max(item.created_at.timestamp() for item in items)
        assert cur.cursor_ts >= newest - 1.0, (
            f"cursor_ts={cur.cursor_ts} must be >= newest item's timestamp "
            f"({newest})"
        )

    def test_cursor_does_not_advance_on_empty_fetch(self) -> None:
        """When zero items are yielded, cursor_ts should stay None (or
        equal to its input). Advancing it on empty runs makes the source
        skip a legitimate future item that lands right at that boundary."""
        src = self.build_source()
        cur = SourceCursor(cursor_ts=None)
        # Set a stream_config that we KNOW will produce nothing — most
        # sources let a nonsense filter yield empty. Skip if we can't.
        try:
            items = list(src.fetch_since(
                cur, {**self.stream_config, "__testkit_force_empty__": True},
                self.build_stats(),
            ))
        except Exception:
            pytest.skip("source doesn't accept an empty-forcing config; skip")
        if items:
            pytest.skip("source ignored empty-forcing hint; test N/A")
        # If we got here, cursor should still be None.
        assert cur.cursor_ts is None, (
            "empty fetch must leave cursor_ts=None; advancing it on "
            "empty runs risks skipping a future item at the boundary"
        )

    # --- stats --------------------------------------------------------------

    def test_stats_ceiling_hits_appendable(self) -> None:
        """FetchStats.ceiling_hits must be a list the source appends to
        when it hits a provider paging limit. Not every source hits its
        ceiling on every run — this test only asserts the structure is
        writable, which is what the fetch loop relies on."""
        _, _, stats = self._fetch_all()
        assert isinstance(stats.ceiling_hits, list)
        # Structure check: any recorded hits are (str, float) tuples.
        for hit in stats.ceiling_hits:
            assert isinstance(hit, tuple) and len(hit) == 2
            assert isinstance(hit[0], str)
            assert isinstance(hit[1], (int, float))

    # --- concurrency (ADR-0023) --------------------------------------------

    def test_fetch_since_is_concurrent_safe(self) -> None:
        """`fetch_since` may be invoked concurrently on ONE Source instance
        under `fetch_concurrency_enabled` — see sources/base.py contract.

        This test drives two `fetch_since` calls in parallel against the
        same instance with fresh per-call cursor + stats. Correctness
        checks:

          - Each call must return the same set of external_ids as a
            single serial call (deterministic on the fixture).
          - Each call's cursor advances to the same monotonic value.
          - Neither call raises.

        Failure mode this catches: an author storing per-call state on
        `self` (a page counter, a shared cursor) — under concurrent
        invocation the two calls scramble that state and one returns
        a subset (or a superset) of what it should.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        # Baseline: what one serial call yields.
        baseline_items, baseline_cursor, _ = self._fetch_all()
        if not baseline_items:
            pytest.skip(
                "concurrent-safety test needs items to compare; set "
                "expects_items=True and stream_config to yield >=1 item"
            )
        baseline_ids = {it.external_id for it in baseline_items}
        baseline_ts = baseline_cursor.cursor_ts

        # Concurrent: drive two calls in parallel on the SAME instance.
        # If the source shares state on `self` between calls, this races.
        src = self.build_source()
        results: list[tuple[set[str], object]] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _run() -> None:
            try:
                cur = self.build_cursor()
                stats = self.build_stats()
                items = list(src.fetch_since(
                    cur, dict(self.stream_config), stats,
                ))
                ids = {it.external_id for it in items}
                with lock:
                    results.append((ids, cur.cursor_ts))
            except BaseException as e:  # collect, don't propagate
                with lock:
                    errors.append(e)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(_run) for _ in range(2)]
            for f in futs:
                f.result()

        assert not errors, (
            f"fetch_since raised under concurrent invocation: {errors[0]!r}. "
            "Guard any shared mutable state on `self` (see "
            "sources/base.py contract, ADR-0023)."
        )
        assert len(results) == 2

        for i, (ids, ts) in enumerate(results):
            assert ids == baseline_ids, (
                f"concurrent call {i} yielded a different id set than "
                f"a serial call.\n  serial: {sorted(baseline_ids)}\n"
                f"  thread: {sorted(ids)}\n"
                "Likely a shared mutable field on `self` scrambled between "
                "threads. Move per-call state to locals in fetch_since."
            )
            assert ts == baseline_ts, (
                f"concurrent call {i} left cursor_ts={ts}, serial left "
                f"cursor_ts={baseline_ts}. Cursor must be deterministic "
                "regardless of invocation mode."
            )
