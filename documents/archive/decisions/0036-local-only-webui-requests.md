# ADR-0036: The web UI refuses foreign hosts and cross-site writes

- **Status:** Accepted
- **Date:** 2026-11-11
- **Area:** infra/security

## Context

The admin UI binds to 127.0.0.1 and has no login (one local operator, per
the non-goals). Its 90 routes include form POSTs that change LLM endpoints,
write `.env`, start runs, install Ollama and delete products. A browser sends
cross-site form POSTs to localhost from any page the operator visits, and a
DNS-rebinding page can read the UI as same-origin.

A concrete chain was found on 2026-11-11: a cross-site POST to
`/connections/assistant_llm` sets the endpoint to
`https://evil.example/anthropic.com/v1`; `llm._resolve_api_key` matched
provider names anywhere in the URL, so the next assistant call sent
`ANTHROPIC_API_KEY` to that host.

## Options considered

1. **Login or per-session token** — strongest, but a new auth surface for a
   single-user local tool, against the non-goals.
2. **CSRF tokens in every form** — correct, but touches every template and
   route, and still leaves DNS rebinding open.
3. **Middleware: Host allowlist on every request, same-origin check on
   writes** — one small module, no template changes, covers both attacks.

## Decision

Option 3, in `webui/security.py`:

- Every request's `Host` must be `127.0.0.1`, `localhost`, `::1`, or a name
  in `PRODUCT_MONITOR_ALLOWED_HOSTS` (environment or `.env`).
- POST/PUT/PATCH/DELETE with an `Origin` (or, failing that, `Referer`) that
  isn't this host:port get 403. Writes with neither header (curl, scripts)
  pass.

Separately, `_resolve_api_key` now matches provider domains against the
endpoint's hostname only (exact or parent domain).

## Rationale

Browsers attach `Origin` to cross-origin POSTs, so checking it blocks the
form-post attack without touching templates. The Host check is the standard
DNS-rebinding defence. Neither adds anything for the operator to configure
on the default install.

## Consequences

- A deliberate server install under another hostname needs
  `PRODUCT_MONITOR_ALLOWED_HOSTS`. Tests set it to `testserver` in
  `tests/conftest.py`.
- A local tool that POSTs from a browser page on another port is refused.
- This is not authentication. Exposing the UI beyond localhost still needs
  its own ADR.

## Revisit if

- The UI is ever served to more than one user, or beyond localhost by
  default.
