"""Tests for the Ollama-server detection / spawn helper.

We mock subprocess + httpx so the tests don't actually launch ollama or
make network calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pipeline import ollama_lifecycle


def test_normalize_base_url_strips_v1():
    assert ollama_lifecycle.normalize_base_url("http://localhost:11434/v1") == "http://localhost:11434"
    assert ollama_lifecycle.normalize_base_url("http://localhost:11434") == "http://localhost:11434"
    assert ollama_lifecycle.normalize_base_url("http://localhost:11434/v1/") == "http://localhost:11434"


def test_normalize_base_url_default_on_empty():
    assert ollama_lifecycle.normalize_base_url("") == "http://localhost:11434"


def test_ensure_running_already_running_short_circuits():
    with patch("pipeline.ollama_lifecycle.is_server_running", return_value=True), \
         patch("pipeline.ollama_lifecycle.list_pulled_models", return_value=["phi4-mini:latest"]), \
         patch("pipeline.ollama_lifecycle.server_version", return_value="0.4.0"), \
         patch("pipeline.ollama_lifecycle.start_server") as start_mock:
        out = ollama_lifecycle.ensure_running("http://localhost:11434")
    assert out["ok"] is True
    assert out["started"] is False
    assert out["models"] == ["phi4-mini:latest"]
    assert out["version"] == "0.4.0"
    start_mock.assert_not_called()  # didn't spawn anything


def test_ensure_running_with_required_model_flag():
    with patch("pipeline.ollama_lifecycle.is_server_running", return_value=True), \
         patch("pipeline.ollama_lifecycle.list_pulled_models", return_value=["phi4-mini:latest"]), \
         patch("pipeline.ollama_lifecycle.server_version", return_value="0.4.0"):
        ok = ollama_lifecycle.ensure_running("http://localhost:11434", required_model="phi4-mini:latest")
        missing = ollama_lifecycle.ensure_running("http://localhost:11434", required_model="llama3.2")
    assert ok["required_pulled"] is True
    assert missing["required_pulled"] is False


def test_ensure_running_no_binary_installed():
    with patch("pipeline.ollama_lifecycle.is_server_running", return_value=False), \
         patch("pipeline.ollama_lifecycle.find_ollama_binary", return_value=None):
        out = ollama_lifecycle.ensure_running("http://localhost:11434")
    assert out["ok"] is False
    assert out["started"] is False
    assert "not installed" in out["message"].lower()


def test_ensure_running_spawns_and_waits():
    spawn_result = {"ok": True, "message": "spawned (pid 1234)", "pid": 1234}
    with patch("pipeline.ollama_lifecycle.is_server_running", return_value=False), \
         patch("pipeline.ollama_lifecycle.find_ollama_binary", return_value="/usr/bin/ollama"), \
         patch("pipeline.ollama_lifecycle.start_server", return_value=spawn_result) as start_mock, \
         patch("pipeline.ollama_lifecycle.wait_for_ready", return_value=True) as wait_mock, \
         patch("pipeline.ollama_lifecycle.list_pulled_models", return_value=["phi4-mini"]), \
         patch("pipeline.ollama_lifecycle.server_version", return_value="0.4.0"):
        out = ollama_lifecycle.ensure_running("http://localhost:11434")
    start_mock.assert_called_once()
    wait_mock.assert_called_once()
    assert out["ok"] is True
    assert out["started"] is True
    assert "phi4-mini" in out["models"]


def test_ensure_running_spawn_times_out():
    spawn_result = {"ok": True, "message": "spawned", "pid": 1234}
    with patch("pipeline.ollama_lifecycle.is_server_running", return_value=False), \
         patch("pipeline.ollama_lifecycle.find_ollama_binary", return_value="/usr/bin/ollama"), \
         patch("pipeline.ollama_lifecycle.start_server", return_value=spawn_result), \
         patch("pipeline.ollama_lifecycle.wait_for_ready", return_value=False):
        out = ollama_lifecycle.ensure_running("http://localhost:11434", timeout_s=5)
    assert out["ok"] is False
    assert out["started"] is True
    assert "didn't become ready" in out["message"]


def test_ensure_running_spawn_fails():
    with patch("pipeline.ollama_lifecycle.is_server_running", return_value=False), \
         patch("pipeline.ollama_lifecycle.find_ollama_binary", return_value="/usr/bin/ollama"), \
         patch("pipeline.ollama_lifecycle.start_server", return_value={"ok": False, "message": "permission denied"}):
        out = ollama_lifecycle.ensure_running("http://localhost:11434")
    assert out["ok"] is False
    assert out["started"] is False


def test_is_server_running_handles_timeout():
    import httpx
    with patch("httpx.get", side_effect=httpx.ConnectTimeout("boom")):
        assert ollama_lifecycle.is_server_running() is False


def test_list_pulled_models_handles_error():
    with patch("httpx.get", side_effect=Exception("boom")):
        assert ollama_lifecycle.list_pulled_models() == []
