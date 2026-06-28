"""Detect / start / probe the Ollama server.

Called from the admin UI when the user picks an Ollama-shaped endpoint on
the LLM routing page so they don't have to manually run `ollama serve`
beforehand. All operations are best-effort and non-fatal — the UI
surfaces the status and lets the user proceed regardless.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.4
_PROBE_TIMEOUT_S = 2.0


def normalize_base_url(url: str) -> str:
    """Strip a trailing /v1 (OpenAI-compat path) so we can hit Ollama's native
    /api/tags etc. e.g. http://localhost:11434/v1 -> http://localhost:11434."""
    url = (url or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url or _DEFAULT_BASE_URL


def find_ollama_binary() -> Optional[str]:
    """Return the absolute path to the ollama executable, or None if not
    installed. Checks PATH first, then known Windows install locations."""
    on_path = shutil.which("ollama")
    if on_path:
        return on_path
    candidates: list[Path] = []
    if sys.platform == "win32":
        candidates += [
            Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
            Path.home() / "AppData" / "Local" / "Programs" / "Ollama" / "ollama.exe",
        ]
    elif sys.platform == "darwin":
        candidates += [Path("/Applications/Ollama.app/Contents/Resources/ollama")]
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def is_server_running(base_url: str = _DEFAULT_BASE_URL) -> bool:
    """Cheap reachability probe. /api/tags is the lightest 200-returning endpoint."""
    base_url = normalize_base_url(base_url)
    try:
        r = httpx.get(f"{base_url}/api/tags", timeout=_PROBE_TIMEOUT_S)
        return r.status_code == 200
    except Exception:
        return False


def list_pulled_models(base_url: str = _DEFAULT_BASE_URL) -> list[str]:
    """Names of models currently pulled into Ollama. Empty list on any error."""
    base_url = normalize_base_url(base_url)
    try:
        r = httpx.get(f"{base_url}/api/tags", timeout=_PROBE_TIMEOUT_S)
        if r.status_code != 200:
            return []
        models = (r.json().get("models") or [])
        return [m.get("name", "") for m in models if m.get("name")]
    except Exception:
        return []


def server_version(base_url: str = _DEFAULT_BASE_URL) -> Optional[str]:
    """Best-effort: returns 'x.y.z' or None."""
    base_url = normalize_base_url(base_url)
    try:
        r = httpx.get(f"{base_url}/api/version", timeout=_PROBE_TIMEOUT_S)
        if r.status_code == 200:
            return (r.json() or {}).get("version")
    except Exception:
        pass
    return None


def start_server() -> dict[str, Any]:
    """Spawn `ollama serve` as a detached background process. Does NOT wait
    for readiness — call wait_for_ready() after. Returns {ok, message, pid?}."""
    bin_path = find_ollama_binary()
    if not bin_path:
        return {
            "ok": False,
            "message": "Ollama is not installed. Get it at https://ollama.com",
        }
    try:
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) if sys.platform == "win32" else 0
        p = subprocess.Popen(
            [bin_path, "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
        )
        return {"ok": True, "message": f"Spawned ollama serve (pid {p.pid})", "pid": p.pid}
    except Exception as e:
        return {"ok": False, "message": f"Failed to spawn ollama serve: {e}"}


def wait_for_ready(
    base_url: str = _DEFAULT_BASE_URL, timeout_s: float = _DEFAULT_TIMEOUT_S
) -> bool:
    """Poll /api/tags until it answers 200 or `timeout_s` elapses."""
    base_url = normalize_base_url(base_url)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if is_server_running(base_url):
            return True
        time.sleep(_POLL_INTERVAL_S)
    return False


def ensure_running(
    base_url: str = _DEFAULT_BASE_URL,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    required_model: Optional[str] = None,
) -> dict[str, Any]:
    """Top-level: detect, start if needed, wait, and report.

    Return shape:
        ok               bool   — did we end with a reachable server?
        started          bool   — did we have to spawn it ourselves?
        message          str    — human-readable summary
        base_url         str    — the URL we probed (normalised)
        version          str?   — Ollama version when reachable
        models           list   — names of pulled models
        required_pulled  bool?  — present iff `required_model` was given
    """
    base_url = normalize_base_url(base_url)

    if is_server_running(base_url):
        models = list_pulled_models(base_url)
        out = {
            "ok": True,
            "started": False,
            "message": "Ollama server already running",
            "base_url": base_url,
            "version": server_version(base_url),
            "models": models,
        }
        if required_model:
            out["required_pulled"] = required_model in models
        return out

    if not find_ollama_binary():
        return {
            "ok": False, "started": False,
            "message": "Ollama is not installed locally. Download at https://ollama.com",
            "base_url": base_url, "version": None, "models": [],
        }

    spawn = start_server()
    if not spawn.get("ok"):
        return {**spawn, "started": False, "base_url": base_url,
                "version": None, "models": []}

    if not wait_for_ready(base_url, timeout_s):
        return {
            "ok": False, "started": True,
            "message": (
                f"Spawned ollama serve but it didn't become ready within {timeout_s:.0f}s. "
                f"Try opening a terminal and running `ollama serve` manually to see what's wrong."
            ),
            "base_url": base_url, "version": None, "models": [],
        }

    models = list_pulled_models(base_url)
    out = {
        "ok": True, "started": True,
        "message": "Started Ollama server",
        "base_url": base_url,
        "version": server_version(base_url),
        "models": models,
    }
    if required_model:
        out["required_pulled"] = required_model in models
    return out
