"""Plugin discovery + registry (POST_V1_PLAN §4.1, ADR-0001).

Three discovery paths, all producing `SourceManifest` + `Source` subclass
pairs:

  1. Built-in — scan `sources/*.py` for modules exporting a `MANIFEST`.
  2. Drop-in — scan `plugins/*.py` and `plugins/<pkg>/plugin.py`.
     Loaded ONLY if `--trust-plugins-dir` flag or `TRUST_PLUGINS_DIR`
     env var is set (see ADR-0008).
  3. Entry points — Python packaging entry points in the
     `customer_feedback.sources` group. Loaded unconditionally (the user
     explicitly `pip install`ed them).

Failure isolation: one broken plugin logs a warning and is skipped;
other plugins still register.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import pkgutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Type

from sources.base import Source, SourceManifest

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BUILTIN_DIR = _REPO_ROOT / "sources"
_DEFAULT_PLUGINS_DIR = _REPO_ROOT / "plugins"
_ENTRY_POINT_GROUP = "customer_feedback.sources"


@dataclass(frozen=True)
class RegisteredPlugin:
    """A discovered plugin: its manifest, its `Source` subclass, and where it
    came from (for debugging)."""

    manifest: SourceManifest
    source_cls: Type[Source]
    origin: str  # "builtin" | "drop_in" | "entry_point"


class Registry:
    """Container for discovered plugins. Populated once at startup by
    `discover()`. Read via `all_plugins()`, `all_manifests()`, `get(plugin_id)`."""

    def __init__(self) -> None:
        self._by_id: dict[str, RegisteredPlugin] = {}

    def add(self, plugin: RegisteredPlugin) -> None:
        pid = plugin.manifest.plugin_id
        if pid in self._by_id:
            existing = self._by_id[pid]
            log.warning(
                "plugin id collision: %s already registered from %s; "
                "ignoring new registration from %s",
                pid, existing.origin, plugin.origin,
            )
            return
        self._by_id[pid] = plugin

    def get(self, plugin_id: str) -> Optional[RegisteredPlugin]:
        return self._by_id.get(plugin_id)

    def all_plugins(self) -> list[RegisteredPlugin]:
        return sorted(self._by_id.values(), key=lambda p: p.manifest.display_name)

    def all_manifests(self) -> list[SourceManifest]:
        return [p.manifest for p in self.all_plugins()]

    def all_ids(self) -> list[str]:
        return sorted(self._by_id.keys())

    def clear(self) -> None:
        """Reset the registry — useful in tests."""
        self._by_id.clear()


# ============================================================================
# Discovery
# ============================================================================


def _extract_from_module(mod, origin: str) -> Optional[RegisteredPlugin]:
    """Given an imported module, extract MANIFEST + Source subclass. Returns
    None if the module doesn't declare a valid plugin (not an error — many
    modules in the source dirs are internal, e.g. base.py itself)."""
    manifest = getattr(mod, "MANIFEST", None)
    if not isinstance(manifest, SourceManifest):
        return None

    # Find a Source subclass — first one wins. Plugin authors should have
    # exactly one Source subclass per module for simplicity.
    source_cls: Optional[Type[Source]] = None
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and issubclass(obj, Source) and obj is not Source:
            source_cls = obj
            break

    if source_cls is None:
        log.warning(
            "plugin module %s declares MANIFEST but no Source subclass; skipping",
            mod.__name__,
        )
        return None

    return RegisteredPlugin(manifest=manifest, source_cls=source_cls, origin=origin)


def _discover_builtin(registry: Registry) -> None:
    """Scan sources/*.py for MANIFEST-bearing modules."""
    import sources as builtin_pkg

    for mod_info in pkgutil.iter_modules(builtin_pkg.__path__):
        if mod_info.name in {"base", "registry", "__init__"}:
            continue
        try:
            mod = importlib.import_module(f"sources.{mod_info.name}")
        except Exception as e:
            log.warning("failed to import built-in source %r: %s", mod_info.name, e)
            continue
        plugin = _extract_from_module(mod, origin="builtin")
        if plugin is not None:
            registry.add(plugin)

    # Also scan subpackages (e.g. sources/scrapecreators/reddit.py)
    for sub_info in pkgutil.iter_modules(builtin_pkg.__path__):
        if not sub_info.ispkg:
            continue
        subpkg = importlib.import_module(f"sources.{sub_info.name}")
        for leaf_info in pkgutil.iter_modules(subpkg.__path__):
            if leaf_info.name in {"base", "client", "__init__"}:
                continue
            try:
                mod = importlib.import_module(
                    f"sources.{sub_info.name}.{leaf_info.name}"
                )
            except Exception as e:
                log.warning(
                    "failed to import built-in source %s.%s: %s",
                    sub_info.name, leaf_info.name, e,
                )
                continue
            plugin = _extract_from_module(mod, origin="builtin")
            if plugin is not None:
                registry.add(plugin)


def _discover_drop_in(registry: Registry, plugins_dir: Path) -> None:
    """Scan plugins_dir for drop-in plugins.

    Two accepted shapes:
      plugins/foo.py                (single-file plugin)
      plugins/foo/plugin.py         (packaged plugin — plugin.py is entry)
    """
    if not plugins_dir.exists():
        return

    # Single-file plugins
    for py_file in sorted(plugins_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue  # __init__.py, __pycache__, etc.
        plugin = _load_plugin_by_path(py_file, origin="drop_in")
        if plugin is not None:
            registry.add(plugin)

    # Packaged plugins (a subdirectory with plugin.py)
    for sub in sorted(plugins_dir.iterdir()):
        if not sub.is_dir():
            continue
        if sub.name.startswith("_") or sub.name == "example":
            # skip __pycache__ and the shipped example scaffold
            # (example lives in plugins/example/ but isn't auto-loaded unless
            # renamed by the plugin author)
            continue
        entry = sub / "plugin.py"
        if not entry.exists():
            continue
        plugin = _load_plugin_by_path(entry, origin="drop_in")
        if plugin is not None:
            registry.add(plugin)


def _load_plugin_by_path(py_file: Path, origin: str) -> Optional[RegisteredPlugin]:
    """Load a Python file from disk by path. Used for drop-in discovery."""
    module_name = f"customer_feedback_plugin_{py_file.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if spec is None or spec.loader is None:
            log.warning("failed to create module spec for %s", py_file)
            return None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    except Exception as e:
        log.warning("failed to load drop-in plugin %s: %s", py_file, e)
        return None
    return _extract_from_module(mod, origin=origin)


def _discover_entry_points(registry: Registry) -> None:
    """Scan Python entry points for pip-installed plugins.

    A plugin package declares in its pyproject.toml:

        [project.entry-points."customer_feedback.sources"]
        my_plugin = "my_plugin_package.plugin"

    We load each declared module and extract MANIFEST + Source.
    """
    try:
        # importlib.metadata is stdlib since 3.8
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return

    try:
        eps = entry_points(group=_ENTRY_POINT_GROUP)
    except TypeError:
        # Older importlib.metadata (Python 3.9) uses different API
        eps = entry_points().get(_ENTRY_POINT_GROUP, [])  # type: ignore[assignment]

    for ep in eps:
        try:
            mod = ep.load()
        except Exception as e:
            log.warning(
                "failed to load entry-point plugin %r: %s", ep.name, e,
            )
            continue
        # Entry point may point at a module OR a manifest object directly.
        if isinstance(mod, SourceManifest):
            log.warning(
                "entry-point plugin %r points at a SourceManifest object; "
                "should point at the plugin module. Skipping.", ep.name,
            )
            continue
        plugin = _extract_from_module(mod, origin="entry_point")
        if plugin is not None:
            registry.add(plugin)


def _trust_plugins_dir_enabled() -> bool:
    """Drop-in plugins load only when explicitly trusted (ADR-0008)."""
    # `TRUST_PLUGINS_DIR` env var — either the literal path OR a truthy flag
    env_val = os.environ.get("TRUST_PLUGINS_DIR", "").strip().lower()
    if env_val in {"", "0", "false", "no", "off"}:
        return False
    return True


def _plugins_dir_from_env() -> Path:
    """Return the drop-in plugin directory. If the trust env var contains a
    path (not a boolean-like value), use it; else use the default repo root
    `plugins/`."""
    env_val = os.environ.get("TRUST_PLUGINS_DIR", "").strip()
    if env_val and env_val.lower() not in {"1", "true", "yes", "on"}:
        return Path(env_val).expanduser().resolve()
    return _DEFAULT_PLUGINS_DIR


def discover(trust_plugins_dir: Optional[bool] = None) -> Registry:
    """Discover all plugins from the three sources.

    Returns a fresh `Registry`. Callers cache it (see `get_registry()`).

    `trust_plugins_dir`: override the env-var check for drop-in loading.
    Useful in tests. If None, reads TRUST_PLUGINS_DIR from env.
    """
    registry = Registry()

    _discover_builtin(registry)

    if trust_plugins_dir is None:
        trust_plugins_dir = _trust_plugins_dir_enabled()
    if trust_plugins_dir:
        _discover_drop_in(registry, _plugins_dir_from_env())
    else:
        log.debug("drop-in plugin scan skipped (TRUST_PLUGINS_DIR not set)")

    _discover_entry_points(registry)

    log.info(
        "plugin discovery complete: %d plugins registered (%s)",
        len(registry.all_plugins()),
        ", ".join(p.manifest.plugin_id for p in registry.all_plugins()),
    )
    return registry


# Lazy singleton — first call runs discovery, subsequent calls return cached.
_registry: Optional[Registry] = None


def get_registry() -> Registry:
    """Return the process-wide registry, running discovery on first call."""
    global _registry
    if _registry is None:
        _registry = discover()
    return _registry


def reset_registry() -> None:
    """Force re-discovery on next `get_registry()`. Useful in tests and
    after plugin file changes."""
    global _registry
    _registry = None
