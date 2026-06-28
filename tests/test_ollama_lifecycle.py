"""Tests for the Ollama-server detection / spawn helper.

We mock subprocess + httpx so the tests don't actually launch ollama or
make network calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
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


# --- tag-aware model matching ----------------------------------------------


def test_is_model_pulled_exact_match():
    assert ollama_lifecycle.is_model_pulled("phi3:latest", ["phi3:latest"]) is True
    assert ollama_lifecycle.is_model_pulled("phi3:latest", ["phi3:14b"]) is False


def test_is_model_pulled_tag_aware_base_matches_latest():
    # User picked "phi3" from recommendations; they have phi3:latest pulled.
    assert ollama_lifecycle.is_model_pulled("phi3", ["phi3:latest"]) is True
    assert ollama_lifecycle.is_model_pulled("llama3", ["llama3:latest", "mistral:latest"]) is True


def test_is_model_pulled_tag_aware_base_matches_any_tag():
    # phi3 (base) matches phi3:14b even though :latest isn't pulled.
    assert ollama_lifecycle.is_model_pulled("phi3", ["phi3:14b"]) is True


def test_is_model_pulled_specific_tag_doesnt_match_other_tag():
    # phi3:14b shouldn't be considered pulled just because phi3:7b is there.
    assert ollama_lifecycle.is_model_pulled("phi3:14b", ["phi3:7b"]) is False


def test_is_model_pulled_empty_required():
    assert ollama_lifecycle.is_model_pulled("", ["phi3:latest"]) is False


def test_is_model_pulled_no_pulls():
    assert ollama_lifecycle.is_model_pulled("phi3", []) is False


def test_ensure_running_required_pulled_uses_tag_aware_match():
    # Server says phi3:latest is pulled; user asks about "phi3" without tag.
    with patch("pipeline.ollama_lifecycle.is_server_running", return_value=True), \
         patch("pipeline.ollama_lifecycle.list_pulled_models",
               return_value=["phi3:latest", "llama3:latest"]), \
         patch("pipeline.ollama_lifecycle.server_version", return_value="0.4.0"):
        out = ollama_lifecycle.ensure_running("http://localhost:11434", required_model="phi3")
    assert out["required_pulled"] is True


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


# --- pull_model -------------------------------------------------------------


class _FakeStreamResp:
    """Minimal context manager that mimics httpx.stream's return value."""
    def __init__(self, lines, status_code=200):
        self._lines = lines
        self.status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_lines(self):
        for ln in self._lines:
            yield ln

    def read(self):
        return b""


def test_pull_model_streams_progress_and_success():
    import json as _json
    lines = [
        _json.dumps({"status": "pulling manifest"}),
        _json.dumps({"status": "downloading", "digest": "sha256:abc", "total": 100, "completed": 50}),
        _json.dumps({"status": "downloading", "digest": "sha256:abc", "total": 100, "completed": 100}),
        _json.dumps({"status": "success"}),
    ]
    with patch("httpx.stream", return_value=_FakeStreamResp(lines)):
        events = list(ollama_lifecycle.pull_model("phi4-mini"))
    assert events[0]["status"] == "pulling manifest"
    assert events[-1]["status"] == "success"
    assert any(e.get("completed") == 50 for e in events)


def test_pull_model_empty_name_yields_error():
    events = list(ollama_lifecycle.pull_model(""))
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert "empty" in events[0]["error"].lower()


def test_pull_model_non_200_yields_error():
    with patch("httpx.stream", return_value=_FakeStreamResp([], status_code=500)):
        events = list(ollama_lifecycle.pull_model("phi4-mini"))
    assert len(events) == 1
    assert events[0]["status"] == "error"
    assert "HTTP 500" in events[0]["error"]


def test_pull_model_http_error_yields_error():
    with patch("httpx.stream", side_effect=httpx.ConnectError("nope")):
        events = list(ollama_lifecycle.pull_model("phi4-mini"))
    assert events[0]["status"] == "error"
    assert "ConnectError" in events[0]["error"]


def test_pull_model_bad_json_line_yields_error_and_stops():
    lines = [
        '{"status": "pulling manifest"}',
        'not-json-at-all',
        '{"status": "success"}',     # should not be reached
    ]
    with patch("httpx.stream", return_value=_FakeStreamResp(lines)):
        events = list(ollama_lifecycle.pull_model("phi4-mini"))
    assert events[0]["status"] == "pulling manifest"
    assert events[-1]["status"] == "error"
    assert not any(e.get("status") == "success" for e in events)
