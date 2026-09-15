"""Service layer between HTTP handlers and pipeline internals (ADR-0026).

Services encapsulate business logic that would otherwise live inline in
FastAPI handlers. Two things this shape enables:

  1. Router split (ADR-0026): a handler in `routers/runs.py` and a
     handler in `routers/wizard.py` both need to list a product's runs.
     Without a service layer they'd duplicate the traversal — with one,
     they call `services.runs.list_runs(product_id)`.
  2. CLI/UI parity: the same code that answers "list this product's
     runs" for the web UI answers it for a future CLI subcommand.

Rules:
  - Services never import FastAPI. They return plain dicts/dataclasses.
  - Services never render templates. Rendering is a router concern.
  - Services may raise domain exceptions; routers translate to HTTP.
"""
