"""Home page, theme setting and product creation.

Split out of webui/app.py (ADR-0026). Shared helpers live in
webui/core.py and are reached as `core.<name>` so a test patch there
reaches every caller.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pipeline.product import available_products, load_product, scaffold_product

from webui import core

router = APIRouter()


@router.post("/settings/theme")
async def settings_theme(request: Request, theme: str = Form("dark"), next: str = Form("/")):
    """Persist Obsidian Console theme preference (ADR-0032)."""
    theme = theme if theme in ("dark", "light") else "dark"
    # Only allow same-origin relative redirects.
    dest = next if isinstance(next, str) and next.startswith("/") and not next.startswith("//") else "/"
    resp = RedirectResponse(url=dest, status_code=303)
    resp.set_cookie(
        "pm_theme",
        theme,
        max_age=60 * 60 * 24 * 365,
        httponly=False,
        samesite="lax",
        path="/",
    )
    return resp


# --- Index: list + create product -------------------------------------------


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    from pipeline import features as _features
    from pipeline import wizard_v2 as _wv2
    products = []
    for pid in available_products():
        try:
            p = load_product(pid)
            products.append({
                "id": p.id,
                "display": p.display,
                "description": p.description,
                "n_sources": len(p.sources),
                "n_areas": len(p.area_ids()),
                "n_features": sum(len(a.get("features") or []) for a in p.enabled_areas()),
                "n_snippets": len(p.snippets),
            })
        except Exception as e:
            products.append({"id": pid, "display": pid, "error": str(e)})

    # In-flight wizard v2 drafts show alongside real products so a half-set-up
    # product doesn't get forgotten between browser sessions. Each draft
    # renders with a "Draft" badge, the current step, and a Resume link that
    # drops the user right back where they left off.
    _STEP_LABELS = {
        "describe":  "Screen 1 — describe",
        "profile":   "Screen 2 — confirm profile",
        "calibrate": "Screen 3 — calibrate",
        "review":    "Screen 4 — review & run",
    }
    _STEP_PROGRESS = {"describe": 25, "profile": 50, "calibrate": 75, "review": 90}
    drafts_v2 = []
    if _features.enabled("wizard_v2_enabled"):
        for d in _wv2.list_drafts(core.PRODUCTS_DIR):
            drafts_v2.append({
                "slug": d.slug,
                "display": d.display or d.slug,
                "description": d.description or d.url_or_description or "",
                "step": d.step or "describe",
                "step_label": _STEP_LABELS.get(d.step or "describe", d.step or ""),
                "progress_pct": _STEP_PROGRESS.get(d.step or "describe", 0),
                "updated_at": d.updated_at,
                "n_aliases": len(d.aliases or []),
                "n_sources_suggested": len(d.suggested_sources or []),
                "n_judgments": len((d.calibration or {}).get("judgments") or {}),
            })

    # first_run_solution.md §4.3 — when the only product is the shipped demo
    # (or none exist), show a welcome screen with two cards ("Try the demo"
    # and "Monitor your product"). The wizard has to be enabled for the
    # second card to lead somewhere useful.
    non_demo_products = [p for p in products if p["id"] != "demo"]
    # Phase 7 flag flip (ADR-0014): with wizard v2 on and no non-demo products
    # AND no in-flight drafts, send new users straight into /wizard. If a
    # draft exists we still land on the list so the user can pick it up.
    if not non_demo_products and not drafts_v2 and _features.enabled("wizard_v2_enabled"):
        return RedirectResponse(url="/wizard", status_code=303)
    first_run = not non_demo_products and _features.enabled("wizard_enabled")
    from webui.services import dashboard as _dash
    dash_ctx = _dash.build_index_context(products, drafts_v2)
    return core.templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "products": dash_ctx["products"],
            "drafts_v2": drafts_v2,
            "first_run": first_run,
            "has_demo": any(p["id"] == "demo" for p in products),
            "install_stats": dash_ctx["install_stats"],
            "attention": dash_ctx["attention"],
            "token_by_product": dash_ctx["token_by_product"],
            "token_other": dash_ctx["token_other"],
            "source_health": dash_ctx["source_health"],
        },
    )


@router.post("/products")
def create_product(
    product_id: str = Form(...),
    display: str = Form(...),
    description: str = Form(""),
):
    product_id = product_id.strip().lower().replace(" ", "-")
    display = display.strip()
    if not product_id or not display:
        raise HTTPException(status_code=400, detail="product_id and display are required")
    try:
        scaffold_product(product_id, display, description.strip())
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=f"/products/{product_id}", status_code=303)
