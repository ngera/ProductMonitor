"""Install Ollama using the official upstream installer for this platform.

Usage:
    python scripts/install_ollama.py

Idempotent: if Ollama is already on PATH or in the default install
location, exits with code 0 and prints where it found it.

Windows: downloads https://ollama.com/download/OllamaSetup.exe to %TEMP%
         and runs it with /SILENT (installs into %LOCALAPPDATA% per-user;
         no UAC prompt).
Linux / macOS: downloads https://ollama.com/install.sh and pipes it
         into sh (the official install path; may prompt for sudo on
         some setups).

After install, you'll typically want to:
    ollama serve            # in a separate terminal
    ollama pull phi4-mini   # ~2 GB; pick whatever small model fits

Or trigger both from the admin UI:
    python -m webui.app     # then open /connections/ollama
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import ollama_lifecycle  # noqa: E402


def main() -> int:
    print(f"[install_ollama] Platform: {sys.platform}")

    existing = ollama_lifecycle.find_ollama_binary()
    if existing:
        print(f"[install_ollama] Already installed at {existing}")
        print("[install_ollama] Nothing to do. Start the server with `ollama serve`.")
        return 0

    print("[install_ollama] Ollama not found on this machine. Installing …")
    result = ollama_lifecycle.install_ollama(progress=lambda m: print(f"[install_ollama] {m}"))

    print()
    if result.get("ok"):
        print(f"[install_ollama] OK: {result.get('message')}")
        if result.get("binary_path"):
            print(f"[install_ollama] Binary: {result['binary_path']}")
        print("[install_ollama] Next steps:")
        print("[install_ollama]   1. Start the server:   ollama serve")
        print("[install_ollama]      (or use the 'Detect / start Ollama server' button at /connections/ollama)")
        print("[install_ollama]   2. Pull a model:       ollama pull phi4-mini")
        return 0

    print(f"[install_ollama] FAILED: {result.get('message')}")
    print("[install_ollama] Fall back to the manual install at https://ollama.com")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
