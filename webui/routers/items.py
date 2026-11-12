"""Item browser for a product.

Split out of webui/app.py (ADR-0026). Shared helpers live in
webui/core.py and are reached as `core.<name>` so a test patch there
reaches every caller.
"""

from __future__ import annotations

import json as _json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pathlib import Path
from pipeline.config import set_current_product
from typing import Optional

from webui import core

router = APIRouter()


# --- Fetched-items browser (debugging) --------------------------------------
#
# Surfaces the per-product DuckDB `items` table so the user can see what
# fetch + normalize produced, plus the optional classification rows. Linked
# from the Runs page. Read-only.


@router.get("/products/{product_id}/items", response_class=HTMLResponse)
def items_list(
    request: Request,
    product_id: str,
    source: Optional[str] = None,
    week: Optional[str] = None,
    relevance: Optional[str] = None,   # 'yes' | 'no' | 'unset'
    offset: int = 0,
):
    from pipeline import storage
    product = core._product_or_404(product_id)
    set_current_product(product)
    limit = 50

    ctx_empty = {
        "request": request, "product": product,
        "items": [], "total": 0,
        "filters": {"source": source or "", "week": week or "",
                    "relevance": relevance or "", "offset": 0, "limit": limit},
        "facets": {"sources": [], "weeks": []},
        "no_warehouse": True,
    }
    if not storage.warehouse_path().exists():
        return core.templates.TemplateResponse("items_list.html", ctx_empty)

    where: list[str] = []
    params: list = []
    if source:
        where.append("source = ?"); params.append(source)
    if week:
        where.append("week_id = ?"); params.append(week)
    if relevance == "yes":
        where.append("is_relevant = TRUE")
    elif relevance == "no":
        where.append("is_relevant = FALSE")
    elif relevance == "unset":
        where.append("is_relevant IS NULL")
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    try:
        total = storage.query(f"SELECT COUNT(*) AS n FROM items{where_sql}", params)[0]["n"]
        rows = storage.query(
            "SELECT id, source, source_display_name, week_id, created_at, author, "
            "url, title, body, is_relevant, filter_status, is_reply, content_type "
            f"FROM items{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        )
        sources = [r["source"] for r in storage.query(
            "SELECT DISTINCT source FROM items ORDER BY source")]
        weeks = [r["week_id"] for r in storage.query(
            "SELECT DISTINCT week_id FROM items ORDER BY week_id DESC")]
    except Exception:
        # Empty / fresh schema, no rows yet.
        ctx_empty["no_warehouse"] = False
        return core.templates.TemplateResponse("items_list.html", ctx_empty)

    return core.templates.TemplateResponse("items_list.html", {
        "request": request, "product": product,
        "items": rows, "total": total,
        "filters": {"source": source or "", "week": week or "",
                    "relevance": relevance or "", "offset": offset, "limit": limit},
        "facets": {"sources": sources, "weeks": weeks},
        "no_warehouse": False,
    })


@router.get("/products/{product_id}/items/detail", response_class=HTMLResponse)
def item_detail(request: Request, product_id: str, item_id: str):
    from pipeline import storage
    product = core._product_or_404(product_id)
    set_current_product(product)

    rows = storage.query("SELECT * FROM items WHERE id = ?", [item_id])
    if not rows:
        raise HTTPException(status_code=404, detail=f"item {item_id} not found")
    item = rows[0]

    cls_rows = storage.query(
        "SELECT * FROM item_classifications WHERE item_id = ?", [item_id])
    classification = cls_rows[0] if cls_rows else None

    areas = storage.query(
        "SELECT area, is_primary FROM item_areas WHERE item_id = ? ORDER BY is_primary DESC, area",
        [item_id])

    # Try to recover the original fetched record from the raw JSONL it came from.
    raw_json = None
    raw_ref = item.get("raw_ref")
    if raw_ref:
        raw_path = Path(raw_ref)
        if raw_path.exists():
            try:
                import json as _json
                with raw_path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        rec = _json.loads(line)
                        if (rec.get("external_id") == item["external_id"]
                                and rec.get("source") == item["source"]):
                            raw_json = rec
                            break
            except Exception:
                pass

    return core.templates.TemplateResponse("item_detail.html", {
        "request": request, "product": product, "item": item,
        "classification": classification, "areas": areas, "raw_json": raw_json,
    })
