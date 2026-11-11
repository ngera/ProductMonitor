# ADR-0011: Single `feedback-monitor` console entry point with subcommand dispatch

- **Status:** Accepted
- **Date:** 2026-07-21
- **Area:** infra

## Context
Before this change the project shipped four separate module entry points
(`python -m pipeline.run`, `python -m webui.app`, `python scripts/init_db.py`,
`python -m pipeline.demo`) and no packaging metadata. That made
`first_run_solution.md`'s promised one-liner —
`uvx feedback-monitor demo` — impossible: `uvx` / `pipx` need a
`pyproject.toml` with a `[project.scripts]` entry to install a callable
binary.

We also wanted users to stop needing to know which module lives where
("was the webui `python -m webui.app` or `python webui/main.py`?"). A
single command with obvious subcommands (`demo`, `ui`, `run`) is the
lower-friction shape.

## Options considered

1. **Add `pyproject.toml` with one `[project.scripts]` entry
   (`feedback-monitor`) that dispatches to a thin `pipeline.cli:main`
   function. Subcommands are `demo | ui | run`.**
   - Pros: one binary to remember; matches how `pip`, `docker`, `git`,
     `hatch` are shaped; `uvx feedback-monitor demo` just works.
   - Cons: introduces a new module (`pipeline.cli`) whose only job is
     dispatch — small file, small maintenance cost.

2. **Three separate `[project.scripts]` entries
   (`feedback-monitor-demo`, `feedback-monitor-ui`, `feedback-monitor-run`).**
   - Pros: no dispatch layer.
   - Cons: three names to remember; `uvx` incantation is longer; feels
     like the pre-git days of `git-status`, `git-commit`, …

3. **Keep the four separate module entry points; document them in the
   README.**
   - Pros: no change.
   - Cons: `uvx` doesn't work; users are back to typing venv+pip
     sequences to try the tool.

## Decision
Ship a `pyproject.toml` with `feedback-monitor = "pipeline.cli:main"`
under `[project.scripts]`. The dispatcher in `pipeline/cli.py`
recognizes three subcommands:

- `demo` → `pipeline.demo.main`
- `ui`   → `webui.app.main`
- `run`  → `pipeline.run.main`

All other args pass through to the target function.

The `[tool.setuptools.package-data]` block ships
`report_templates/`, `config/*.yaml`, `products/demo/`, and
`data/demo/` inside the wheel so a `uvx` install can serve the demo
without a source checkout.

## Rationale
- The `git`-style one-command-many-subcommands pattern is the shape
  users already recognize.
- The dispatcher is thin (~40 LoC) and can be tested without a wheel
  build — just invoke `pipeline.cli:main(["demo"])`.
- `uvx feedback-monitor demo` is the only one-liner in the README that
  matters for adoption. Making it work is worth the small packaging
  overhead.

## Consequences
- **Easy**: adding new subcommands (each is a single dispatch entry
  and a `main(argv)` in whatever module owns it). Ship the tool as
  a wheel or a source tarball. Have `uvx` / `pipx` / `pip install .`
  all Just Work.
- **Hard**: cross-cutting features that need shared state (e.g., a
  global `--verbose` flag) — you'd have to either handle them in the
  dispatcher or in each subcommand.
- **Locked in**: the console name `feedback-monitor`. Renaming would
  break every user's shell history and the docs.

## Revisit if
- We split the project into multiple pip packages (e.g., a separate
  webui plugin) — at that point each package likely wants its own
  entry point, and the dispatcher becomes cross-package coordination
  code.
- Users start requesting shell-completion — Click / Typer make this
  trivial, so a switch might be worth it. Current dispatcher is
  intentionally stdlib-only to keep the demo bundle's dep footprint
  minimal.
