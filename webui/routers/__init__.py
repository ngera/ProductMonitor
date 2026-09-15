"""FastAPI router modules for the admin webui (ADR-0026).

Each router owns a URL prefix (/admin/*, /connections/*, /products/*/runs/*, …)
and is mounted from `webui/app.py` via `app.include_router()`. The main
app.py is the composition root; it does NOT define routes directly for
prefixes that have a dedicated router.

Handlers should stay thin — parse form/params → call a `webui.services.*`
function → render a template. Business logic lives in the service layer,
not in the handler, so the CLI (`pipeline.run`, wizard scripts) and the
UI can share it.
"""
