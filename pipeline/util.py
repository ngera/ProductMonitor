"""Shared helpers: week ids, JSONL I/O, simhash."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import structlog

log = structlog.get_logger()


def safe_error_text(err: BaseException | str, *, limit: int = 800) -> str:
    """Stringify an error for logs without risking UnicodeEncodeError.

    Windows consoles default to cp1252; LLM validation errors often embed
    emoji / non-BMP chars from model output. Replace unencodable codepoints
    so a single log line never aborts the pipeline.
    """
    raw = err if isinstance(err, str) else f"{type(err).__name__}: {err}"
    try:
        text = raw.encode("utf-8", errors="replace").decode("utf-8")
    except Exception:
        text = repr(raw)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def week_id_for(dt: datetime) -> str:
    """ISO week id, e.g. '2026-W22', interpreted in the operator's local
    timezone so item weeks stay aligned with what the user considers
    'this week'. Without this, a late-Sunday UTC-midnight rollover puts
    today's items in last week's bucket while the run targets next week.

    If `dt` is tz-aware (e.g. fetched item timestamps in UTC), convert
    to local first. If naive, treat as already local.
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone()
    iso = dt.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def current_week_id() -> str:
    """ISO week id for now in the operator's local timezone (see week_id_for)."""
    return week_id_for(datetime.now().astimezone())


# --- JSONL -------------------------------------------------------------------


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    raise TypeError(f"not JSON serializable: {type(o)}")


def append_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, default=_json_default, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


# --- simhash (title dedup / fallback grouping) -------------------------------

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def simhash(text: str, bits: int = 64) -> int:
    """64-bit simhash over word tokens. Stable & deterministic."""
    if not text:
        return 0
    vector = [0] * bits
    for tok in _tokens(text):
        h = int.from_bytes(_stable_hash(tok), "big") % (1 << bits)
        for i in range(bits):
            vector[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if vector[i] > 0:
            out |= 1 << i
    return out


def _stable_hash(s: str) -> bytes:
    import hashlib

    return hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest()


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")
