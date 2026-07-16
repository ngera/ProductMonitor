"""Shared pytest fixtures (POST_V1_PLAN §4.12).

Provides:
- Repo root on sys.path so `pipeline`, `sources`, `webui` import
- `mock_llm` — deterministic LLM adapter for unit tests
- `temp_product_dir` — isolated product directory for tests that mutate
- `temp_warehouse` — isolated DuckDB warehouse
- `stub_env` — set env vars for one test, restore on teardown
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import pytest

# Repo root on sys.path so package imports resolve when running via
# `pytest -q` from anywhere.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


# ----------------------------------------------------------------------------
# Deterministic mock LLM adapter
# ----------------------------------------------------------------------------


class MockLLMAdapter:
    """Records calls; returns pre-configured responses.

    Use `.expect(prompt_contains=..., response=...)` to queue responses.
    Use `.calls` to inspect what was sent.

    Deterministic — same input always gives the same output within a single
    test. If a call comes in that no expectation matches, raises AssertionError
    so tests fail loudly on unexpected LLM invocations.
    """

    def __init__(self) -> None:
        self._expectations: list[tuple[Callable[[str], bool], Any]] = []
        self.calls: list[dict[str, Any]] = []

    def expect(self, *, prompt_contains: str | None = None, response: Any) -> None:
        """Queue a response for any call whose prompt contains the given substring.

        If `prompt_contains` is None, matches any call. Later `expect()` calls
        take precedence over earlier ones for overlapping matches — the last
        registered matcher wins.
        """
        def matcher(prompt: str) -> bool:
            if prompt_contains is None:
                return True
            return prompt_contains in prompt
        self._expectations.append((matcher, response))

    def call(self, *, prompt: str = "", **kwargs: Any) -> Any:
        """Simulated LLM call. Records the invocation and returns the matching
        expectation's response (last-registered wins)."""
        self.calls.append({"prompt": prompt, **kwargs})
        for matcher, response in reversed(self._expectations):
            if matcher(prompt):
                return response
        raise AssertionError(
            f"MockLLMAdapter: unexpected call with prompt={prompt[:120]!r}; "
            f"queue any response first via .expect(...)"
        )


@pytest.fixture
def mock_llm() -> MockLLMAdapter:
    """Fresh MockLLMAdapter per test."""
    return MockLLMAdapter()


# ----------------------------------------------------------------------------
# Isolated product directory
# ----------------------------------------------------------------------------


@pytest.fixture
def temp_product_dir(tmp_path: Path) -> Path:
    """Copy the 'windows-media-platform' product into a temp dir so tests can
    mutate freely without polluting the real repo.

    Returns the path to the copied product's directory.
    """
    src = _REPO_ROOT / "products" / "windows-media-platform"
    if not src.exists():
        pytest.skip("windows-media-platform product not present; skipping")
    dst = tmp_path / "windows-media-platform"
    shutil.copytree(src, dst)
    return dst


# ----------------------------------------------------------------------------
# Isolated DuckDB warehouse
# ----------------------------------------------------------------------------


@pytest.fixture
def temp_warehouse(tmp_path: Path) -> Path:
    """Fresh DuckDB warehouse per test. Returns the file path.

    Use with pipeline.storage.warehouse_path() overridden — or call
    scripts.init_db functions directly against this path.
    """
    return tmp_path / "warehouse.duckdb"


# ----------------------------------------------------------------------------
# Env-var stubbing
# ----------------------------------------------------------------------------


@pytest.fixture
def stub_env(monkeypatch: pytest.MonkeyPatch):
    """Set env vars for one test; monkeypatch auto-restores.

    Usage:
        def test_foo(stub_env):
            stub_env("REDDIT_CLIENT_ID", "abc")
    """
    def _stub(name: str, value: str) -> None:
        monkeypatch.setenv(name, value)
    return _stub


# ----------------------------------------------------------------------------
# Feature flag helpers
# ----------------------------------------------------------------------------


@pytest.fixture
def clear_feature_cache():
    """Clear feature-flag cache so tests can mutate config/features.yaml or
    per-product overrides and see fresh state.

    Usage:
        def test_flag(clear_feature_cache):
            clear_feature_cache()
            # ... edit features.yaml ...
            clear_feature_cache()
            assert features.enabled("my_flag")
    """
    from pipeline import features
    features.clear_cache()
    yield features.clear_cache
    features.clear_cache()
