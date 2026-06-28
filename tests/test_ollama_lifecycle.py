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


# --- install_ollama ---------------------------------------------------------


def test_install_short_circuits_when_already_installed():
    with patch("pipeline.ollama_lifecycle.find_ollama_binary", return_value="/usr/bin/ollama"):
        out = ollama_lifecycle.install_ollama()
    assert out["ok"] is True
    assert "already installed" in out["message"].lower()
    assert out["binary_path"] == "/usr/bin/ollama"


def test_install_windows_happy_path():
    # First find returns None (pre-install), second returns the installed path.
    finds = iter([None, r"C:\Users\u\AppData\Local\Programs\Ollama\ollama.exe"])
    proc_ok = MagicMock(returncode=0, stderr="")

    with patch("pipeline.ollama_lifecycle.find_ollama_binary",
               side_effect=lambda: next(finds)), \
         patch("pipeline.ollama_lifecycle._download", return_value=250 * 1024 * 1024), \
         patch("pipeline.ollama_lifecycle.subprocess.run", return_value=proc_ok), \
         patch("pipeline.ollama_lifecycle.sys") as sys_mock, \
         patch("pathlib.Path.unlink"):
        sys_mock.platform = "win32"
        out = ollama_lifecycle.install_ollama()

    assert out["ok"] is True
    assert out["platform"] == "win32"
    assert "Installed" in out["message"]
    assert out["binary_path"].endswith("ollama.exe")
    assert out["bytes_downloaded"] == 250 * 1024 * 1024


def test_install_windows_installer_fails():
    finds = iter([None, None])
    proc_fail = MagicMock(returncode=2, stderr="user cancelled")

    with patch("pipeline.ollama_lifecycle.find_ollama_binary",
               side_effect=lambda: next(finds)), \
         patch("pipeline.ollama_lifecycle._download", return_value=42), \
         patch("pipeline.ollama_lifecycle.subprocess.run", return_value=proc_fail), \
         patch("pipeline.ollama_lifecycle.sys") as sys_mock, \
         patch("pathlib.Path.unlink"):
        sys_mock.platform = "win32"
        out = ollama_lifecycle.install_ollama()

    assert out["ok"] is False
    assert "code 2" in out["message"]


def test_install_windows_installer_runs_but_binary_not_found():
    finds = iter([None, None])
    proc_ok = MagicMock(returncode=0, stderr="")

    with patch("pipeline.ollama_lifecycle.find_ollama_binary",
               side_effect=lambda: next(finds)), \
         patch("pipeline.ollama_lifecycle._download", return_value=42), \
         patch("pipeline.ollama_lifecycle.subprocess.run", return_value=proc_ok), \
         patch("pipeline.ollama_lifecycle.sys") as sys_mock, \
         patch("pathlib.Path.unlink"):
        sys_mock.platform = "win32"
        out = ollama_lifecycle.install_ollama()

    assert out["ok"] is False
    assert "wasn't found" in out["message"]


def test_install_unix_happy_path():
    finds = iter([None, "/usr/local/bin/ollama"])
    proc_ok = MagicMock(returncode=0, stderr="")
    script_body = b"#!/bin/sh\necho install\n"
    mock_resp = MagicMock()
    mock_resp.read.return_value = script_body
    mock_resp.__enter__ = lambda self: self
    mock_resp.__exit__ = lambda *a: None

    with patch("pipeline.ollama_lifecycle.find_ollama_binary",
               side_effect=lambda: next(finds)), \
         patch("pipeline.ollama_lifecycle.urllib.request.urlopen", return_value=mock_resp), \
         patch("pipeline.ollama_lifecycle.subprocess.run", return_value=proc_ok), \
         patch("pipeline.ollama_lifecycle.sys") as sys_mock:
        sys_mock.platform = "linux"
        out = ollama_lifecycle.install_ollama()

    assert out["ok"] is True
    assert out["platform"] == "linux"
    assert out["binary_path"] == "/usr/local/bin/ollama"


def test_install_unsupported_platform():
    with patch("pipeline.ollama_lifecycle.find_ollama_binary", return_value=None), \
         patch("pipeline.ollama_lifecycle.sys") as sys_mock:
        sys_mock.platform = "haiku"   # not supported
        out = ollama_lifecycle.install_ollama()
    assert out["ok"] is False
    assert "Unsupported platform" in out["message"]


def test_install_propagates_download_failure():
    import urllib.error

    finds = iter([None, None])
    with patch("pipeline.ollama_lifecycle.find_ollama_binary",
               side_effect=lambda: next(finds)), \
         patch("pipeline.ollama_lifecycle._download",
               side_effect=urllib.error.URLError("connection refused")), \
         patch("pipeline.ollama_lifecycle.sys") as sys_mock:
        sys_mock.platform = "win32"
        out = ollama_lifecycle.install_ollama()
    assert out["ok"] is False
    assert "Download failed" in out["message"]
