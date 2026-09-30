"""Detect / install / start / probe the Ollama server.

Called from the admin UI (Connections → Ollama → "Install / Start") and
from scripts/install_ollama.py. All operations are best-effort and
non-fatal — the UI surfaces the status and lets the user proceed
regardless.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Optional

import httpx

_DEFAULT_BASE_URL = "http://localhost:11434"
_DEFAULT_TIMEOUT_S = 15.0
_POLL_INTERVAL_S = 0.4
_PROBE_TIMEOUT_S = 2.0

# Reuse one httpx client for readiness + tag probes. `httpx.get()` builds a
# fresh client + TCP handshake each call; hitting the LLM setup page fires
# multiple probes in quick succession, and a shared client saves ~50 ms per.
_HTTP_CLIENT: httpx.Client | None = None


def _http() -> httpx.Client:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        _HTTP_CLIENT = httpx.Client(timeout=_PROBE_TIMEOUT_S)
    return _HTTP_CLIENT


def normalize_base_url(url: str) -> str:
    """Strip a trailing /v1 (OpenAI-compat path) so we can hit Ollama's native
    /api/tags etc. e.g. http://localhost:11434/v1 -> http://localhost:11434."""
    url = (url or "").strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[:-3]
    return url or _DEFAULT_BASE_URL


def resolve_base_url(url: str | None = None) -> str:
    """Normalize ``url`` and rewrite loopback → ``host.docker.internal`` when
    the webui runs inside Docker so it can reach Ollama on the host.

    ``OLLAMA_HOST`` (host[:port], no path) overrides everything — same
    contract as the assistant-LLM wizard defaults.
    """
    override = (os.environ.get("OLLAMA_HOST") or "").strip()
    if override:
        return normalize_base_url(override if "://" in override else f"http://{override}")

    base = normalize_base_url(url or _DEFAULT_BASE_URL)
    if os.path.exists("/.dockerenv"):
        for host in ("localhost", "127.0.0.1", "0.0.0.0"):
            needle = f"://{host}"
            if needle in base:
                return base.replace(needle, "://host.docker.internal", 1)
    return base


def default_openai_compat_endpoint() -> str:
    """OpenAI-compat base (`…/v1`) for routing / assistant config defaults."""
    return resolve_base_url(_DEFAULT_BASE_URL).rstrip("/") + "/v1"


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
        r = _http().get(f"{base_url}/api/tags")
        return r.status_code == 200
    except Exception:
        return False


def list_pulled_models(base_url: str = _DEFAULT_BASE_URL) -> list[str]:
    """Names of models currently pulled into Ollama. Empty list on any error.
    Each name is the full Ollama tag, e.g. `phi3:latest`, `llama3.1:8b`."""
    base_url = normalize_base_url(base_url)
    try:
        r = _http().get(f"{base_url}/api/tags")
        if r.status_code != 200:
            return []
        models = (r.json().get("models") or [])
        return [m.get("name", "") for m in models if m.get("name")]
    except Exception:
        return []


def is_model_pulled(required: str, pulled: list[str]) -> bool:
    """Tag-aware match. `phi3` matches `phi3:latest`; `phi3:14b` matches
    only `phi3:14b`. Lets the user pick a recommended base name without
    having to remember whether they pulled it with or without an explicit
    tag."""
    if not required:
        return False
    if required in pulled:
        return True
    if ":" not in required:
        for p in pulled:
            base = p.split(":", 1)[0]
            if base == required:
                return True
    return False


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


# --- Pull -------------------------------------------------------------------


def pull_model(name: str, base_url: str = _DEFAULT_BASE_URL) -> Iterator[dict[str, Any]]:
    """Stream progress events from Ollama's POST /api/pull.

    Yields the raw JSON-line events from Ollama (`{"status": "...", "digest":
    "...", "total": N, "completed": M}`). Caller is responsible for forwarding
    them to the UI. On any error we yield a synthetic `{"status": "error",
    "error": ...}` event and stop — never raises.

    Pull can take minutes for multi-GB models; no overall timeout — Ollama
    keeps the connection open until done. The HTTP-level read timeout is set
    high enough that idle gaps between progress events don't kill it.
    """
    name = (name or "").strip()
    if not name:
        yield {"status": "error", "error": "empty model name"}
        return
    url = f"{normalize_base_url(base_url)}/api/pull"
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0)
    try:
        with httpx.stream("POST", url, json={"name": name, "stream": True}, timeout=timeout) as r:
            if r.status_code != 200:
                yield {"status": "error", "error": f"HTTP {r.status_code}: {r.read()[:200]!r}"}
                return
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    yield {"status": "error", "error": f"bad json: {line[:200]!r}"}
                    return
    except httpx.HTTPError as e:
        yield {"status": "error", "error": f"{type(e).__name__}: {e}"}


# --- Install ----------------------------------------------------------------


_WINDOWS_INSTALLER_URL = "https://ollama.com/download/OllamaSetup.exe"
_UNIX_INSTALL_SCRIPT_URL = "https://ollama.com/install.sh"


def _download(url: str, dst: Path, *, timeout_s: float = 600.0) -> int:
    """Stream-download `url` to `dst`. Returns bytes written."""
    req = urllib.request.Request(url, headers={"User-Agent": "product-monitor"})
    with urllib.request.urlopen(req, timeout=timeout_s) as r, open(dst, "wb") as f:
        total = 0
        while True:
            chunk = r.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)
            total += len(chunk)
    return total


def install_ollama(*, progress: Optional[Any] = None) -> dict[str, Any]:
    """Install Ollama from the official upstream installer for this platform.

    Windows: download OllamaSetup.exe to %TEMP%, run with /SILENT.
    macOS/Linux: download install.sh, pipe into `sh`.

    `progress` if provided is a callable taking a single status string —
    used by the CLI script and the UI endpoint to surface progress.

    Returns a status dict (always; never raises):
        ok            bool   — did install finish (return code 0)?
        message       str    — human-readable summary
        binary_path   str?   — path to ollama executable after install
        bytes_downloaded int?
        platform      str    — "win32" | "darwin" | "linux"
    """
    p = sys.platform

    def _log(msg: str) -> None:
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    # Already installed? Nothing to do.
    existing = find_ollama_binary()
    if existing:
        return {
            "ok": True, "message": f"Ollama already installed at {existing}",
            "binary_path": existing, "platform": p,
        }

    try:
        if p == "win32":
            return _install_windows(_log)
        if p in ("darwin", "linux"):
            return _install_unix(_log)
        return {
            "ok": False, "message": f"Unsupported platform: {p}",
            "binary_path": None, "platform": p,
        }
    except urllib.error.URLError as e:
        return {
            "ok": False, "message": f"Download failed: {e}",
            "binary_path": None, "platform": p,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "message": "Installer timed out",
            "binary_path": None, "platform": p,
        }
    except Exception as e:
        return {
            "ok": False, "message": f"{type(e).__name__}: {e}",
            "binary_path": None, "platform": p,
        }


def _install_windows(log) -> dict[str, Any]:
    log(f"Downloading {_WINDOWS_INSTALLER_URL} …")
    tmp_dir = Path(tempfile.gettempdir())
    installer = tmp_dir / "OllamaSetup.exe"
    n = _download(_WINDOWS_INSTALLER_URL, installer)
    log(f"Downloaded {n / (1024 * 1024):.1f} MB to {installer}")

    log("Running silent install (no UAC prompt — installs to %LOCALAPPDATA%) …")
    # OllamaSetup.exe is built with NSIS / Inno; /SILENT is the standard quiet flag.
    proc = subprocess.run(
        [str(installer), "/SILENT"],
        capture_output=True, text=True, timeout=300,
    )
    installer.unlink(missing_ok=True)

    if proc.returncode != 0:
        return {
            "ok": False,
            "message": f"Installer returned code {proc.returncode}. stderr: {proc.stderr[-300:]}",
            "binary_path": None, "platform": "win32",
            "bytes_downloaded": n,
        }
    binary = find_ollama_binary()
    if not binary:
        return {
            "ok": False,
            "message": "Installer ran but ollama.exe wasn't found afterward. Try opening a new terminal.",
            "binary_path": None, "platform": "win32",
            "bytes_downloaded": n,
        }
    return {
        "ok": True, "message": f"Installed Ollama at {binary}",
        "binary_path": binary, "platform": "win32",
        "bytes_downloaded": n,
    }


def _install_unix(log) -> dict[str, Any]:
    log(f"Downloading {_UNIX_INSTALL_SCRIPT_URL} …")
    req = urllib.request.Request(_UNIX_INSTALL_SCRIPT_URL,
                                 headers={"User-Agent": "product-monitor"})
    with urllib.request.urlopen(req, timeout=60) as r:
        script = r.read().decode("utf-8")
    log(f"Got install.sh ({len(script)} bytes). Running via sh.")

    proc = subprocess.run(
        ["sh"], input=script, capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        return {
            "ok": False,
            "message": f"install.sh returned code {proc.returncode}. stderr: {proc.stderr[-300:]}",
            "binary_path": None, "platform": sys.platform,
            "bytes_downloaded": len(script),
        }
    binary = find_ollama_binary()
    return {
        "ok": True, "message": f"Installed Ollama at {binary or '(check $PATH)'}",
        "binary_path": binary, "platform": sys.platform,
        "bytes_downloaded": len(script),
    }


# --- Top-level --------------------------------------------------------------


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
    base_url = resolve_base_url(base_url)

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
            out["required_pulled"] = is_model_pulled(required_model, models)
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
        out["required_pulled"] = is_model_pulled(required_model, models)
    return out
