"""Cache-refresh API endpoint (ADR-0026).

Owns `/api/refresh` — called by editor pages that mutate config
(features, prompts, sources) to force the pipeline caches to reload.
Cheap; hits `pipeline.product.clear_cache()` and returns {"ok": True}.
"""

from __future__ import annotations

from fastapi import APIRouter

from pipeline.product import clear_cache

router = APIRouter()


@router.post("/api/refresh")
def refresh_caches() -> dict:
    clear_cache()
    return {"ok": True}
