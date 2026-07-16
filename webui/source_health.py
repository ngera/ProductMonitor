"""Source health computation (POST_V1_PLAN §4.2).

Two views:
  a) Pre-run readiness — per source instance, is the env var configured?
     Reads SourceManifest.connection_fields × os.environ.

  b) Post-run health — what actually happened for each source instance in
     a completed run? Reads the run's errors[] + FetchStats. Distinguishes
     init failures (missing key), rate-limit halts, and completeness gaps.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class SourceReadiness:
    """Per-source-instance readiness at run trigger time."""

    instance_id: str          # e.g., 'reddit-1'
    plugin_id: str            # e.g., 'reddit'
    display_name: str         # e.g., 'Reddit'
    n_streams: int
    n_streams_paused: int
    status: str               # 'ready' | 'partial' | 'missing' | 'unknown_plugin'
    missing_env_vars: list[str]
    hint: str                 # actionable text
    fix_url: str              # deep link to the connection form


@dataclass
class SourceHealth:
    """Per-source-instance health after a run."""

    instance_id: str
    plugin_id: str
    display_name: str
    status: str               # 'ok' | 'error' | 'partial'
    items_fetched: int
    streams_ok: int
    streams_failed: int
    error_summary: str        # one-line summary of the first error
    ceiling_hits: int         # count of stream_name entries in FetchStats.ceiling_hits


def compute_readiness(product_sources: list[dict]) -> list[SourceReadiness]:
    """For each source instance in product.sources, compute readiness."""
    from sources.registry import get_registry

    reg = get_registry()
    env = _read_env_snapshot()
    result: list[SourceReadiness] = []

    for src in product_sources:
        instance_id = src.get("id", "?")
        plugin_id = src.get("type", "?")
        streams = src.get("streams") or []
        n_streams = len(streams)
        n_streams_paused = sum(1 for s in streams if s.get("paused"))

        plugin = reg.get(plugin_id)
        if plugin is None:
            result.append(SourceReadiness(
                instance_id=instance_id,
                plugin_id=plugin_id,
                display_name=plugin_id,
                n_streams=n_streams,
                n_streams_paused=n_streams_paused,
                status="unknown_plugin",
                missing_env_vars=[],
                hint=f"Plugin '{plugin_id}' is not registered in this build.",
                fix_url="",
            ))
            continue

        manifest = plugin.manifest
        required_env = [f for f in manifest.connection_fields if f.required or f.type == "secret"]
        missing = [f.name for f in required_env if not env.get(f.name, "").strip()]

        if not required_env:
            status = "ready"
            hint = "No auth needed."
        elif not missing:
            status = "ready"
            hint = "All credentials configured."
        elif len(missing) == len(required_env):
            status = "missing"
            hint = (
                f"Set {', '.join(missing)} in .env. "
                f"Deep link: /connections/{plugin_id}"
            )
        else:
            status = "partial"
            hint = f"Missing: {', '.join(missing)}. Deep link: /connections/{plugin_id}"

        result.append(SourceReadiness(
            instance_id=instance_id,
            plugin_id=plugin_id,
            display_name=manifest.display_name,
            n_streams=n_streams,
            n_streams_paused=n_streams_paused,
            status=status,
            missing_env_vars=missing,
            hint=hint,
            fix_url=f"/connections/{plugin_id}" if required_env else "",
        ))

    return result


def compute_health(run_json: dict, product_sources: list[dict]) -> list[SourceHealth]:
    """For each source instance in product_sources, compute health from the
    completed run's JSON payload (errors[], counters, completeness).

    The run JSON currently records errors at the top level as strings like:
      "source reddit-1 init failed: 'REDDIT_CLIENT_SECRET'"
    We parse those to extract per-instance status.
    """
    from sources.registry import get_registry

    reg = get_registry()
    errors: list[str] = run_json.get("errors", []) or []
    completeness = run_json.get("completeness") or {}
    ceiling_hits_all = completeness.get("ceiling_hits") or []

    # Build a quick lookup: instance_id → error message (first one)
    errors_by_instance: dict[str, str] = {}
    for err in errors:
        # "source <instance_id> init failed: <detail>"
        if "source " in err and " init failed:" in err:
            try:
                after = err.split("source ", 1)[1]
                inst_id, _, detail = after.partition(" init failed:")
                errors_by_instance.setdefault(inst_id.strip(), detail.strip())
            except Exception:
                pass

    result: list[SourceHealth] = []
    for src in product_sources:
        instance_id = src.get("id", "?")
        plugin_id = src.get("type", "?")
        streams = src.get("streams") or []
        plugin = reg.get(plugin_id)
        display = plugin.manifest.display_name if plugin else plugin_id

        # Approximate: any ceiling_hit whose first-field starts with instance's
        # stream names counts against this instance.
        stream_names = {s.get("name") for s in streams if s.get("name")}
        instance_ceiling_hits = 0
        for entry in ceiling_hits_all:
            if not entry:
                continue
            first = entry[0] if isinstance(entry, (list, tuple)) else str(entry)
            # ceiling_hits entries are typed like "reddit-rss:r-windows11-new:*"
            # or "hn:hn-windows-media-platform:sound" — starts with a stream name
            for sn in stream_names:
                if sn and (first.startswith(sn) or f":{sn}:" in first):
                    instance_ceiling_hits += 1
                    break

        err = errors_by_instance.get(instance_id, "")
        if err:
            status = "error"
        elif instance_ceiling_hits > 0:
            status = "partial"
        else:
            status = "ok"

        result.append(SourceHealth(
            instance_id=instance_id,
            plugin_id=plugin_id,
            display_name=display,
            status=status,
            items_fetched=0,   # TODO: parse from counters when we have per-instance counters
            streams_ok=len(streams) - (1 if err else 0),
            streams_failed=1 if err else 0,
            error_summary=err[:200] if err else "",
            ceiling_hits=instance_ceiling_hits,
        ))

    return result


def _read_env_snapshot() -> dict[str, str]:
    """Read .env into a dict (does not mutate os.environ). Used by readiness
    to check secret configuration without polluting the process env."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return dict(os.environ)  # fallback to real env

    try:
        from dotenv import dotenv_values
        vals = dotenv_values(str(env_path))
        # Merge with os.environ, .env taking precedence for keys it defines
        merged = dict(os.environ)
        for k, v in vals.items():
            if v is not None:
                merged[k] = v
        return merged
    except Exception:
        return dict(os.environ)
