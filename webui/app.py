"""FastAPI admin app, bound to 127.0.0.1.

Admin tool to manage all products (a.k.a. search topics — Product -> Area
-> Feature hierarchy) and view their generated reports. Routes cover:
product list/create + dashboard, taxonomy / sources / prompts /
llm-routing editors, snippet add/list/edit, run trigger + report viewer.
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from webui import core

app = FastAPI(title="ProductMonitor")

# ADR-0036 — refuse foreign Host headers and cross-site writes.
from webui.security import LocalOnlyMiddleware  # noqa: E402

app.add_middleware(LocalOnlyMiddleware)

# Routers split out before the bulk move (ADR-0026).
from webui.routers import health as _health_router

from webui.routers import reports as _reports_router

from webui.routers import api_refresh as _api_refresh_router

app.include_router(_health_router.router)

app.include_router(_reports_router.router)

app.include_router(_api_refresh_router.router)

if core.STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(core.STATIC_DIR)), name="static")

# --- Wizard v2 router (wizard redesign Phase 3) ------------------------------
# Mounted here so /wizard* routes come from webui/wizard.py rather than being
# tangled into this file. Feature-flagged by `wizard_v2_enabled`; when the
# flag is off, every route in the router returns 403.
from webui import wizard as _wizard_v2_router  # noqa: E402

app.include_router(_wizard_v2_router.router)


@app.on_event("startup")


def _start_scheduler() -> None:  # pragma: no cover — thread lifecycle
    if core._scheduler_started:
        return
    from webui import scheduler_runtime
    scheduler_runtime.start()
    core._scheduler_started = True

# ADR-0026 — every route lives in a router. Include order follows the old
# definition order in this file, which FastAPI matching depends on
# (e.g. /products/{id}/edit/meta before /products/{id}/edit/{section}).
from webui.routers import home as _home_router  # noqa: E402
app.include_router(_home_router.router)
from webui.routers import admin as _admin_router  # noqa: E402
app.include_router(_admin_router.router)
from webui.routers import product_wizard as _product_wizard_router  # noqa: E402
app.include_router(_product_wizard_router.router)
from webui.routers import products as _products_router  # noqa: E402
app.include_router(_products_router.router)
from webui.routers import connections as _connections_router  # noqa: E402
app.include_router(_connections_router.router)
from webui.routers import product_config as _product_config_router  # noqa: E402
app.include_router(_product_config_router.router)
from webui.routers import snippets as _snippets_router  # noqa: E402
app.include_router(_snippets_router.router)
from webui.routers import runs as _runs_router  # noqa: E402
app.include_router(_runs_router.router)
from webui.routers import items as _items_router  # noqa: E402
app.include_router(_items_router.router)

# /products/{id}/reports/{week}/* moved to webui.routers.reports (ADR-0026).
# _serve_report / _serve_report_subpath moved into that router module.


# /api/refresh moved to webui.routers.api_refresh (ADR-0026).


# --- Entry point ------------------------------------------------------------


def serve(host: str = "127.0.0.1", port: int = 8766, reload: bool = False) -> None:
    uvicorn.run(
        "webui.app:app" if reload else app,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


def main() -> None:
    # Cross-platform preflight (see pipeline/preflight.py).
    from pipeline import preflight
    preflight.check()

    import argparse

    ap = argparse.ArgumentParser(description="ProductMonitor — local admin UI")
    ap.add_argument("--host", default="127.0.0.1", help="Bind address (local-only default)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--reload", action="store_true", help="Auto-reload on file change (dev)")
    args = ap.parse_args()
    serve(args.host, args.port, args.reload)


if __name__ == "__main__":
    main()
