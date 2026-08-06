"""Startup preflight — cheap cross-platform checks with actionable hints.

Runs at most once per process. Silent when everything looks fine; prints
a single warning line + fix hint when a common gotcha is detected so
users get an actionable message BEFORE the actual failure surfaces
cryptically deeper in the stack.

Currently checks:
- Python version >= 3.11 (the project's declared minimum in pyproject.toml)
- TLS library (macOS system Python ships LibreSSL 2.x which fails TLS
  handshakes against modern APIs like Reddit, Anthropic, OpenAI)

Hook it into every user-facing entry point (`pipeline/cli.py::main`,
`pipeline/run.py::main`, `webui/app.py::main`). The module-level guard
makes duplicate calls a no-op, so hooking generously is fine.
"""

from __future__ import annotations

import sys


_ran = False


def check() -> None:
    """Run startup checks once per process. Idempotent + cheap (~1 ms)."""
    global _ran
    if _ran:
        return
    _ran = True

    _check_python_version()
    _check_tls_library()


def _check_python_version() -> None:
    if sys.version_info < (3, 11):
        print(
            f"[preflight] Python {sys.version_info.major}.{sys.version_info.minor} "
            "detected; this project targets Python 3.11+. Older versions may "
            "fail cryptically on typing / stdlib usage.",
            file=sys.stderr,
        )


def _check_tls_library() -> None:
    """Warn about ancient TLS libraries.

    macOS system Python (`/usr/bin/python3` on older macOS) ships with
    LibreSSL 2.x which lacks modern TLS 1.2/1.3 negotiation in some
    builds — causing SSL handshake failures against Reddit, Anthropic,
    OpenAI, and similar endpoints. Homebrew (`brew install python@3.11`)
    and python.org installers bundle a modern OpenSSL.
    """
    try:
        import ssl
    except ImportError:
        return

    version_str = getattr(ssl, "OPENSSL_VERSION", "") or ""
    version_num = getattr(ssl, "OPENSSL_VERSION_NUMBER", 0) or 0

    # OpenSSL 1.1.1 = 0x1010100f. Anything earlier is EOL and problematic
    # against endpoints that require TLS 1.2+ (which is now essentially
    # all of them).
    is_ancient_openssl = 0 < version_num < 0x1010100F
    # LibreSSL 2.x reports as "LibreSSL 2.x.y" with a lower version_num.
    # LibreSSL 3.x on modern macOS works fine.
    is_libressl_2 = version_str.startswith("LibreSSL 2.")

    if not (is_ancient_openssl or is_libressl_2):
        return

    print(
        f"[preflight] Old TLS library detected: {version_str}. "
        "Modern HTTPS endpoints (Reddit, Anthropic, OpenAI) may fail SSL "
        "handshakes.",
        file=sys.stderr,
    )
    if sys.platform == "darwin":
        print(
            "  On macOS, install Python via Homebrew and re-create your venv:\n"
            "    brew install python@3.11\n"
            "    /opt/homebrew/bin/python3.11 -m venv .venv\n"
            "    source .venv/bin/activate\n"
            "    pip install -e .[dev]",
            file=sys.stderr,
        )
    else:
        print(
            "  Install a Python from python.org, pyenv, or your package "
            "manager that bundles a modern OpenSSL (>= 1.1.1).",
            file=sys.stderr,
        )
