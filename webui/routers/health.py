"""Health-check router (ADR-0026).

Owns `/healthz` — cross-product liveness signal for unattended installs.
See documents/OPERATING.md for the operator-facing contract and
ADR-0025 for the run-notification story that motivates it.

Deliberately unauthenticated — the webui binds to 127.0.0.1 so a
healthz call is already local-only. If someone bind-mounts the port
more widely (docker-compose.yml warns against this), that's their
call to make.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from pipeline.config import app_config
from pipeline.product import available_products
from webui.services import runs as runs_service

router = APIRouter()


_HEALTHZ_STALE_HOURS_DEFAULT = 24 * 8      # 8 days: weekly cadence + slack
_HEALTHZ_STALE_HOURS_KEY = "healthz_stale_hours"


@router.get("/healthz")
def healthz():
    """Cross-product liveness signal for unattended installs.

    200 OK when at least one product has a successful run within the
    staleness threshold (default 8 days), OR when no products are
    configured yet (fresh install, nothing to fail).

    503 SERVICE UNAVAILABLE when every configured product has stale or
    missing successful runs — a monitor should page.
    """
    now = datetime.now(timezone.utc)
    stale_hours = float(
        app_config().get("scheduler", {}).get(
            _HEALTHZ_STALE_HOURS_KEY, _HEALTHZ_STALE_HOURS_DEFAULT,
        )
    )
    stale_seconds = stale_hours * 3600.0

    products = available_products()
    if not products:
        # Fresh install — no products means nothing is scheduled means
        # nothing has failed to run.
        return {"status": "ok", "products": 0, "last_run_age_seconds": None}

    per_product: dict[str, Optional[float]] = {}
    freshest: Optional[float] = None
    for pid in products:
        age = runs_service.last_successful_run_age_seconds(pid, now)
        per_product[pid] = age
        if age is not None and (freshest is None or age < freshest):
            freshest = age

    body: dict = {
        "status": "ok",
        "products": len(products),
        "last_run_age_seconds": freshest,
        "per_product": per_product,
        "stale_threshold_seconds": stale_seconds,
    }
    if freshest is None or freshest > stale_seconds:
        body["status"] = "stale"
        return JSONResponse(status_code=503, content=body)
    return body
