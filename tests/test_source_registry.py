"""Tests for sources/registry.py plugin discovery (POST_V1_PLAN §4.1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from sources import registry
from sources.base import FieldSpec, Source, SourceManifest


def test_discovery_finds_hn_builtin(monkeypatch):
    """The `hn` plugin has been migrated to the MANIFEST pattern; discovery
    must find it as a built-in."""
    registry.reset_registry()
    # Ensure trust flag is off so drop-in scan doesn't add noise
    monkeypatch.delenv("TRUST_PLUGINS_DIR", raising=False)
    reg = registry.discover(trust_plugins_dir=False)
    hn = reg.get("hn")
    assert hn is not None
    assert hn.origin == "builtin"
    assert hn.manifest.plugin_id == "hn"
    assert hn.manifest.display_name == "Hacker News"
    assert hn.manifest.identifier_field == "search_queries"
    # Source class is registered too
    assert issubclass(hn.source_cls, Source)
    assert hn.source_cls.name == "hn"


def test_discovery_ignores_base_and_registry_modules(monkeypatch):
    """base.py and registry.py themselves don't declare a MANIFEST — the
    scanner must skip them silently, not warn or crash."""
    registry.reset_registry()
    monkeypatch.delenv("TRUST_PLUGINS_DIR", raising=False)
    reg = registry.discover(trust_plugins_dir=False)
    # No plugin_id 'base' or 'registry' in the list
    assert reg.get("base") is None
    assert reg.get("registry") is None


def test_discovery_skips_drop_in_without_trust_flag(monkeypatch, tmp_path: Path):
    """Drop-in scan requires explicit TRUST_PLUGINS_DIR opt-in (ADR-0008)."""
    # Create a fake drop-in plugin file
    plugin_file = tmp_path / "fake_plugin.py"
    plugin_file.write_text(
        "from sources.base import Source, SourceManifest, SourceCursor, FetchStats\n"
        "MANIFEST = SourceManifest(plugin_id='fake_dropin', display_name='Fake')\n"
        "class FakeSource(Source):\n"
        "    name = 'fake_dropin'\n"
        "    def fetch_since(self, cursor, config, stats):\n"
        "        return iter([])\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "_DEFAULT_PLUGINS_DIR", tmp_path)
    monkeypatch.delenv("TRUST_PLUGINS_DIR", raising=False)

    reg = registry.discover(trust_plugins_dir=False)
    assert reg.get("fake_dropin") is None, "drop-in must not load without trust flag"


def test_discovery_loads_drop_in_with_trust_flag(monkeypatch, tmp_path: Path):
    """With TRUST_PLUGINS_DIR set, drop-in plugins do load."""
    plugin_file = tmp_path / "fake_plugin.py"
    plugin_file.write_text(
        "from sources.base import Source, SourceManifest, SourceCursor, FetchStats\n"
        "MANIFEST = SourceManifest(plugin_id='fake_trusted', display_name='Fake Trusted')\n"
        "class FakeSource(Source):\n"
        "    name = 'fake_trusted'\n"
        "    def fetch_since(self, cursor, config, stats):\n"
        "        return iter([])\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "_DEFAULT_PLUGINS_DIR", tmp_path)

    reg = registry.discover(trust_plugins_dir=True)
    found = reg.get("fake_trusted")
    assert found is not None
    assert found.origin == "drop_in"


def test_registry_add_ignores_id_collision():
    """Second registration of the same plugin_id must be ignored, first wins."""
    reg = registry.Registry()
    manifest_a = SourceManifest(plugin_id="same", display_name="A")
    manifest_b = SourceManifest(plugin_id="same", display_name="B")

    class SourceA(Source):
        name = "same"
        def fetch_since(self, cursor, config, stats):
            return iter([])

    class SourceB(Source):
        name = "same"
        def fetch_since(self, cursor, config, stats):
            return iter([])

    reg.add(registry.RegisteredPlugin(manifest=manifest_a, source_cls=SourceA, origin="builtin"))
    reg.add(registry.RegisteredPlugin(manifest=manifest_b, source_cls=SourceB, origin="drop_in"))

    got = reg.get("same")
    assert got is not None
    assert got.manifest.display_name == "A"  # first wins


def test_all_ids_sorted():
    reg = registry.Registry()
    for pid in ["z", "a", "m"]:
        reg.add(registry.RegisteredPlugin(
            manifest=SourceManifest(plugin_id=pid, display_name=pid.upper()),
            source_cls=type(f"S_{pid}", (Source,), {"name": pid, "fetch_since": lambda self, c, cfg, s: iter([])}),
            origin="builtin",
        ))
    assert reg.all_ids() == ["a", "m", "z"]
