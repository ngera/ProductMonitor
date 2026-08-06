"""Write to `.env` AND sync `os.environ` in one step.

Fixes the classic "saved via UI, still 401 on the next request" bug —
`dotenv.set_key` only writes to the file. The running process keeps
serving stale values from its startup-cached `os.environ` until it's
restarted, so any UI save that only calls `set_key` silently requires a
process restart to take effect.

The helpers below make sure both the file and `os.environ` stay in sync
so any code path reading `os.environ.get(...)` immediately sees the new
value on the next request — no restart, no docker-compose down/up.

Every UI POST handler that writes to `.env` should go through here.
There are two writers in the codebase (`webui/app.py` for /connections,
`webui/wizard.py` for /wizard/llm); both import from this module so a
future third writer keeps the "write + sync" pairing correct by default.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

from dotenv import set_key as _dotenv_set, unset_key as _dotenv_unset


PathLike = Union[str, Path]


def set_var(env_file: PathLike, name: str, value: str,
             *, quote_mode: str = "auto") -> None:
    """Write `name=value` to `env_file` AND mirror it into `os.environ`.

    Empty `name` is a no-op. The `env_file` path is required (rather than
    computed) so tests using `isolated_env` fixtures — which monkeypatch
    the caller's `ENV_FILE_PATH` / `_env_path()` to a tmp path — keep
    working. Both callers in the codebase (webui/app.py + webui/wizard.py)
    have their own canonical path constant they pass in.
    """
    if not name:
        return
    path = Path(env_file)
    path.touch(exist_ok=True)
    _dotenv_set(str(path), name, value, quote_mode=quote_mode)
    # Sync the running process's env so the next `os.environ.get(name)`
    # call anywhere in the codebase sees the new value on the SAME
    # request — no restart, no docker compose down/up needed.
    os.environ[name] = value


def unset_var(env_file: PathLike, name: str) -> None:
    """Remove `name` from `env_file` and from `os.environ`."""
    if not name:
        return
    path = Path(env_file)
    if path.exists():
        try:
            _dotenv_unset(str(path), name)
        except Exception:
            # dotenv.unset_key can raise on quirky formatting; the file
            # write is best-effort but the os.environ pop below is what
            # actually protects the running process from serving stale.
            pass
    os.environ.pop(name, None)


def set_or_unset(env_file: PathLike, name: str, value: str,
                  *, quote_mode: str = "auto") -> None:
    """Convenience: empty value ⇒ `unset_var`, else `set_var`. Matches
    the common UI pattern where blanking a form field means "clear this
    secret" rather than "store empty string"."""
    if not name:
        return
    if (value or "").strip():
        set_var(env_file, name, value, quote_mode=quote_mode)
    else:
        unset_var(env_file, name)
