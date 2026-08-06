"""Per-product scheduler — fires pipeline runs on a cadence.

Design decisions (2026-07-29 user Q&A):
  1. Daemon lives inside the webui process (background thread launched at
     uvicorn startup). See webui/scheduler_runtime.py for the tick loop.
  2. Time-of-day is explicit: schedule.hour + schedule.minute (UTC). A
     day is considered "due" the first tick after that clock time.
  3. Overlap: if a `.running` marker exists for the product when the tick
     fires, we skip and write a placeholder run-log entry so the runs
     list shows the skipped tick + reason. No queuing.

Per-product state lives in `products/<pid>/schedule.yaml`:

    enabled: true
    cadence: weekly          # daily | weekly | bi_weekly | monthly
    time_window: last_week   # from_last_cursor | last_week | last_month | last_quarter
    hour: 9                  # UTC hour of day (0-23)
    minute: 0                # UTC minute (0-59)
    last_fired_at: '2026-07-25T09:00:00+00:00'

The module is I/O-agnostic: everything except `save` / `load` is pure so
the caller (webui) can dry-run the "should this fire?" logic in tests.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml


# ---------------------------------------------------------------------------
# Constants + validators
# ---------------------------------------------------------------------------

CADENCES = ("daily", "weekly", "bi_weekly", "monthly")
TIME_WINDOWS = ("from_last_cursor", "last_week", "last_month", "last_quarter")

# Cadence interval in days. Monthly uses 30 as a rolling approximation —
# calendar-month accuracy would need month arithmetic, which is more code
# than the value delivers for a scheduler that already fires within a
# 60-second wake window of "due".
_CADENCE_DAYS = {
    "daily":     1,
    "weekly":    7,
    "bi_weekly": 14,
    "monthly":   30,
}


# Time-window → pipeline --time-mode mapping. The schedule concept
# "from_last_cursor" is the same as the pipeline's "incremental" mode.
TIME_WINDOW_TO_TIME_MODE = {
    "from_last_cursor": "incremental",
    "last_week":        "last_week",
    "last_month":       "last_month",
    "last_quarter":     "last_quarter",
}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class Schedule:
    enabled: bool = False
    cadence: str = "weekly"
    time_window: str = "last_week"
    hour: int = 9
    minute: int = 0
    last_fired_at: Optional[str] = None   # ISO-8601 UTC

    def validate(self) -> "Schedule":
        """Raise ValueError if any field is invalid. Returns self so callers
        can chain `Schedule(...).validate()`."""
        if self.cadence not in CADENCES:
            raise ValueError(
                f"cadence must be one of {CADENCES!r}, got {self.cadence!r}"
            )
        if self.time_window not in TIME_WINDOWS:
            raise ValueError(
                f"time_window must be one of {TIME_WINDOWS!r}, "
                f"got {self.time_window!r}"
            )
        if not (0 <= int(self.hour) <= 23):
            raise ValueError(f"hour must be 0-23, got {self.hour!r}")
        if not (0 <= int(self.minute) <= 59):
            raise ValueError(f"minute must be 0-59, got {self.minute!r}")
        # Normalize ints (yaml may load them as strings via manual editing).
        self.hour = int(self.hour)
        self.minute = int(self.minute)
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def path_for(product_id: str, products_dir: Optional[Path] = None) -> Path:
    from pipeline.product import PRODUCTS_DIR
    base = products_dir or PRODUCTS_DIR
    return base / product_id / "schedule.yaml"


def load(product_id: str, products_dir: Optional[Path] = None) -> Schedule:
    """Return the persisted schedule, or an all-defaults `Schedule(enabled=False)`
    when no schedule.yaml exists yet. Missing / malformed files degrade to the
    default rather than raising — the caller (UI + scheduler) treats "no
    schedule" as "disabled".
    """
    p = path_for(product_id, products_dir)
    if not p.exists():
        return Schedule()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return Schedule()
    if not isinstance(raw, dict):
        return Schedule()
    return Schedule(
        enabled=bool(raw.get("enabled", False)),
        cadence=str(raw.get("cadence") or "weekly"),
        time_window=str(raw.get("time_window") or "last_week"),
        hour=int(raw.get("hour", 9)),
        minute=int(raw.get("minute", 0)),
        last_fired_at=raw.get("last_fired_at") or None,
    )


def save(product_id: str, schedule: Schedule,
         products_dir: Optional[Path] = None) -> None:
    """Persist to products/<pid>/schedule.yaml (validated first)."""
    schedule.validate()
    p = path_for(product_id, products_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(schedule.to_dict(), sort_keys=False, allow_unicode=True,
                       default_flow_style=False),
        encoding="utf-8",
    )
    tmp.replace(p)


def mark_fired(product_id: str, fired_at: datetime,
               products_dir: Optional[Path] = None) -> None:
    """Update last_fired_at on the persisted schedule (idempotent-ish;
    overwrites whatever's there). Preserves every other field."""
    sched = load(product_id, products_dir)
    sched.last_fired_at = fired_at.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )
    save(product_id, sched, products_dir)


# ---------------------------------------------------------------------------
# Firing decision
# ---------------------------------------------------------------------------


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def next_due_at(schedule: Schedule, *, now: Optional[datetime] = None) -> datetime:
    """The earliest UTC datetime at which this schedule should fire next.

    On a never-fired schedule → the next hour:minute on/after `now`.
    On a fired schedule → `last_fired_at` bumped by the cadence interval, then
    snapped to the schedule's hour:minute of that day.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    last = _parse_iso(schedule.last_fired_at)
    interval = timedelta(days=_CADENCE_DAYS[schedule.cadence])

    if last is None:
        # First fire = today at hour:minute if that's still in the future,
        # else tomorrow at hour:minute. Keeps a fresh schedule from firing
        # immediately.
        candidate = now.replace(
            hour=schedule.hour, minute=schedule.minute,
            second=0, microsecond=0,
        )
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    # Bump last_fired by cadence, then snap to hour:minute (the interval
    # arithmetic can drift if a prior fire happened off-schedule, e.g. a
    # manual trigger via mark_fired).
    base = last + interval
    snapped = base.replace(
        hour=schedule.hour, minute=schedule.minute,
        second=0, microsecond=0,
    )
    return snapped


def should_fire(schedule: Schedule, *, now: Optional[datetime] = None) -> bool:
    """True when `now >= next_due_at(schedule)` AND the schedule is enabled.

    Pure — no I/O. Callers do the actual firing + last_fired update.
    """
    if not schedule.enabled:
        return False
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return now >= next_due_at(schedule, now=now)


# ---------------------------------------------------------------------------
# Overlap detection + skipped-run placeholder
# ---------------------------------------------------------------------------


def in_flight_run_id(logs_dir: Path) -> Optional[str]:
    """Return the marker id of a currently-running pipeline for this product,
    or None. A `.running` file with no matching `.json` (terminal state)
    indicates in-flight.

    Kept intentionally simple: we don't try to check whether the PID is still
    alive — the UI's run-listing code does that cleanup already. Worst case
    we skip one tick when a run just finished but its marker wasn't yet
    cleaned; the next tick 60s later will fire cleanly.
    """
    if not logs_dir.exists():
        return None
    for p in sorted(logs_dir.glob("*.running")):
        stem = p.stem
        if not (logs_dir / f"{stem}.json").exists():
            return stem
    return None


def write_skipped_placeholder(
    logs_dir: Path, product_id: str, reason: str,
    now: Optional[datetime] = None,
) -> str:
    """Write a run-log JSON documenting a schedule tick that was skipped.

    Shows up in the runs list with status "scheduled_skipped" and the reason.
    Returns the marker id used.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    marker_id = "sched-" + now.strftime("%Y%m%dT%H%M%S")
    logs_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": marker_id,
        "product_id": product_id,
        "status": "scheduled_skipped",
        "started_at": now.isoformat(timespec="seconds"),
        "finished_at": now.isoformat(timespec="seconds"),
        "stage_durations": {},
        "counters": {},
        "completeness": {},
        "errors": [f"scheduled tick skipped: {reason}"],
    }
    (logs_dir / f"{marker_id}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8",
    )
    return marker_id
