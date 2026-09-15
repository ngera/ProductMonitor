"""Run-log inspection service (ADR-0026).

Every route that reads `data/<product>/run_logs/*.json` — the runs UI,
`/healthz`, the runs-detail page, the wizard's post-run smoke test —
goes through this module. Previously each of those hit the filesystem
directly with its own copy of the traversal; this is where those copies
consolidate.

Pure Python: no FastAPI, no template rendering. Raises `FileNotFoundError`
when a run doesn't exist. Callers translate to HTTP.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pipeline.config import app_config, resolve_path


def product_data_root(product_id: str) -> Path:
    """Per-product data directory: `data/<product_id>/`."""
    return resolve_path(app_config()["paths"]["data_root"]) / product_id


def run_logs_dir(product_id: str) -> Path:
    """Per-product run-log directory: `data/<product_id>/run_logs/`."""
    return product_data_root(product_id) / "run_logs"


def reports_root_for(product_id: str) -> Path:
    """Per-product rendered-report root: `reports/<product_id>/`."""
    return resolve_path(app_config()["paths"]["reports_root"]) / product_id


def run_id_started_at(run_id: str) -> Optional[datetime]:
    """Parse a UTC datetime out of a run_id like `ui-20260915T120000-abc123`
    or `sched-20260915T120000-abc123`. Returns None when the id doesn't
    match the timestamp convention (legacy or manually-crafted ids)."""
    m = re.search(r"(\d{8}T\d{6})", run_id)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def last_successful_run_age_seconds(
    product_id: str, now: datetime,
) -> Optional[float]:
    """Age of the most recent status=success run for one product, in
    seconds. None when the product has never produced a successful run."""
    logs_dir = run_logs_dir(product_id)
    if not logs_dir.exists():
        return None
    latest: Optional[datetime] = None
    for jf in logs_dir.glob("*.json"):
        try:
            payload = json.loads(jf.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (payload.get("status") or "") != "success":
            continue
        # Prefer the run_id timestamp (embedded, monotonic); fall back to
        # the file's mtime for legacy ids that don't encode a timestamp.
        started = run_id_started_at(payload.get("run_id") or jf.stem)
        if started is None:
            try:
                started = datetime.fromtimestamp(jf.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
        if latest is None or started > latest:
            latest = started
    if latest is None:
        return None
    return (now - latest).total_seconds()
