"""Console entry point — `feedback-monitor <subcommand>`.

Subcommands:
  demo   Offline replay demo (POST_V1 §4.12 replay adapter). Runs the whole
         pipeline against a bundled HN capture with recorded LLM responses,
         then opens the report in a browser. No keys, no network.
  ui     Start the local admin webui (webui.app.serve).
  run    Run the weekly pipeline (pipeline.run.main).

Exposed via `[project.scripts]` in pyproject.toml so `uvx feedback-monitor`
and `pipx run feedback-monitor` both work with no venv setup.
"""

from __future__ import annotations

import sys
from typing import Optional


_HELP = """\
Usage: feedback-monitor <command> [args...]

Commands:
  demo   Offline replay demo (no keys, ~2 min). Opens the report in a browser.
  ui     Start the local admin webui at http://127.0.0.1:8765.
  run    Run the weekly pipeline (equivalent to `python -m pipeline.run`).

Run `feedback-monitor <command> --help` for command-specific options.
"""


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0

    cmd, rest = argv[0], argv[1:]

    if cmd == "demo":
        from pipeline.demo import main as demo_main
        return demo_main(rest)
    if cmd == "ui":
        from webui.app import main as ui_main
        # ui_main() reads sys.argv itself; splice our sub-args in.
        sys.argv = ["feedback-monitor ui", *rest]
        ui_main()
        return 0
    if cmd == "run":
        from pipeline.run import main as run_main
        return run_main(rest)

    print(f"unknown command: {cmd!r}\n", file=sys.stderr)
    print(_HELP, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
