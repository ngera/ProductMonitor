"""Static report file server (ADR-0026).

Owns `/products/{id}/reports/{week}/*` — serves the rendered HTML digest
and its nested assets (chart PNGs under `data/`, etc.) from
`reports/<product>/<week>/`.

Path-traversal defense: `_serve_report_subpath` resolves the target and
verifies containment inside the week's root. Simple filename routes
reject anything containing `/` or a leading `.`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from webui.services import runs as runs_service

router = APIRouter()


@router.get("/products/{product_id}/reports/{week_id}/")
def report_index(product_id: str, week_id: str):
    return _serve_report(product_id, week_id, "index.html")


@router.get("/products/{product_id}/reports/{week_id}/{filename}")
def report_file(product_id: str, week_id: str, filename: str):
    if "/" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    return _serve_report(product_id, week_id, filename)


# Nested subresources (chart PNGs under data/, etc.) — matches any path
# with slashes so `<img src="data/trend_bugs.png">` resolves under
# reports/<product>/<week>/. Path traversal is defended via the resolve()
# containment check inside _serve_report_subpath.
@router.get("/products/{product_id}/reports/{week_id}/{subpath:path}")
def report_subpath(product_id: str, week_id: str, subpath: str):
    return _serve_report_subpath(product_id, week_id, subpath)


def _serve_report(product_id: str, week_id: str, filename: str):
    path = runs_service.reports_root_for(product_id) / week_id / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail=f"no report file at {path}")
    return FileResponse(str(path))


def _serve_report_subpath(product_id: str, week_id: str, subpath: str):
    # Refuse anything that could escape the report dir.
    if ".." in subpath.replace("\\", "/").split("/"):
        raise HTTPException(status_code=400, detail="invalid path")
    week_root = (runs_service.reports_root_for(product_id) / week_id).resolve()
    target = (week_root / subpath).resolve()
    try:
        target.relative_to(week_root)
    except ValueError:
        raise HTTPException(status_code=400, detail="path escapes report dir")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail=f"no report file at {target}")
    return FileResponse(str(target))
