"""`product-monitor demo` — the offline "aha" run (first_run_solution.md §3).

Runs the full pipeline against a bundled, real HN capture with recorded LLM
responses (ADR-0010 replay adapter). Zero network, zero keys, ~2 minutes.

Steps:
  1. Confirm the demo bundle is on disk (raw JSONL + llm_replay.jsonl).
  2. Invoke `pipeline.run.main(--product demo --week <bundled week>
     --skip-fetch)` — this exercises normalize / filter / relevance /
     classify / group / score / aggregate / render exactly like a real run.
  3. Auto-open the report in the default browser (pipeline.run already
     does this when stdout is a tty).
  4. Print the next-step call-to-action.

If the demo bundle is missing, we point the user at
`scripts/build_demo_replay.py` to regenerate it — same script used by CI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional


DEMO_WEEK = "2026-W29"
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent

DEMO_PRODUCT_DIR = _PACKAGE_ROOT / "products" / "demo"
DEMO_RAW_JSONL = _PACKAGE_ROOT / "data" / "demo" / "raw" / "hn" / DEMO_WEEK / "hn-demo-notion.jsonl"
DEMO_REPLAY_JSONL = _PACKAGE_ROOT / "data" / "demo" / "llm_replay.jsonl"


_NEXT_STEP = """
Next: monitor your own product  ->  product-monitor ui
      (that opens the local admin webui at http://127.0.0.1:8766)
"""


def _bundle_check() -> Optional[str]:
    """Return an error string if the bundle is missing, else None."""
    for path, label in (
        (DEMO_PRODUCT_DIR / "product.yaml", "products/demo/product.yaml"),
        (DEMO_RAW_JSONL, str(DEMO_RAW_JSONL.relative_to(_PACKAGE_ROOT))),
        (DEMO_REPLAY_JSONL, str(DEMO_REPLAY_JSONL.relative_to(_PACKAGE_ROOT))),
    ):
        if not path.exists():
            return (
                f"demo bundle is incomplete — missing {label}.\n"
                f"Rebuild it with: python scripts/build_demo_replay.py"
            )
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="product-monitor demo",
        description=(
            "Run the offline demo pipeline (replayed HN chatter about Notion) "
            "and open the report in your browser. No keys required."
        ),
    )
    parser.add_argument(
        "--no-open-browser", dest="open_browser", action="store_false",
        default=True, help="Suppress the auto-open browser behavior (useful in CI).",
    )
    parser.add_argument(
        "--week", default=DEMO_WEEK,
        help=f"Override the demo week id (default: {DEMO_WEEK}, matches the bundle).",
    )
    args = parser.parse_args(argv)

    err = _bundle_check()
    if err:
        print(f"[demo] {err}", file=sys.stderr)
        return 2

    print("[demo] running offline demo pipeline (no network, no keys) …")

    # Hand off to the shared pipeline entry point. --skip-fetch keeps the
    # bundled raw JSONL as the input; the demo product's llm_routing.yaml
    # points classify + relevance at replay://demo.
    from pipeline.run import main as run_main

    run_argv = [
        "--product", "demo",
        "--week", args.week,
        "--skip-fetch",
    ]
    if not args.open_browser:
        run_argv.append("--no-open-browser")
    else:
        # Force auto-open even when stdout isn't a tty (e.g., `uvx …` on
        # Windows PowerShell where isatty can be misleading).
        run_argv.append("--open-browser")

    rc = run_main(run_argv)
    if rc == 0:
        print(_NEXT_STEP)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
