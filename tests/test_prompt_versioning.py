"""Tests for pipeline/prompt_versioning.py (POST_V1_PLAN §4.5)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from pipeline.prompt_versioning import (
    bump_version,
    ensure_versioned,
    list_history,
    load_version,
)


def _write_prompts(pdir: Path, blob: dict) -> None:
    (pdir / "prompts.yaml").write_text(yaml.safe_dump(blob), encoding="utf-8")


def test_ensure_versioned_returns_empty_when_missing(tmp_path):
    assert ensure_versioned(tmp_path) == {}


def test_ensure_versioned_retrofits_id_and_version(tmp_path):
    """Existing prompts.yaml without id/version gets retrofitted."""
    _write_prompts(tmp_path, {"relevance": {"system": "s"}})
    blob = ensure_versioned(tmp_path)
    assert blob["id"]
    assert blob["version"] == 1
    assert blob["updated_at"]

    # Round-trip: re-read after ensure — same values.
    blob2 = ensure_versioned(tmp_path)
    assert blob2["version"] == 1
    assert blob2["id"] == blob["id"]


def test_ensure_versioned_preserves_existing_version(tmp_path):
    """If id + version already exist, don't overwrite them."""
    _write_prompts(tmp_path, {
        "id": "custom_id", "version": 5, "updated_at": "2026-01-01",
        "relevance": {"system": "s"},
    })
    blob = ensure_versioned(tmp_path)
    assert blob["id"] == "custom_id"
    assert blob["version"] == 5


def test_bump_version_archives_previous(tmp_path):
    _write_prompts(tmp_path, {"id": "p", "version": 3, "relevance": {"system": "old"}})
    new_blob = {"id": "p", "relevance": {"system": "new"}}
    new_version = bump_version(tmp_path, new_blob)

    assert new_version == 4
    # History file at v3
    archive = tmp_path / "prompts.yaml.history" / "v3.yaml"
    assert archive.exists()
    archived = yaml.safe_load(archive.read_text(encoding="utf-8"))
    assert archived["relevance"]["system"] == "old"

    # Current file is the new version
    current = yaml.safe_load((tmp_path / "prompts.yaml").read_text(encoding="utf-8"))
    assert current["version"] == 4
    assert current["relevance"]["system"] == "new"


def test_bump_version_starts_at_1_when_no_previous(tmp_path):
    """First save on a fresh product → v1."""
    new_version = bump_version(tmp_path, {"relevance": {"system": "first"}})
    # No previous file → no archive needed
    assert new_version == 1
    current = yaml.safe_load((tmp_path / "prompts.yaml").read_text(encoding="utf-8"))
    assert current["version"] == 1


def test_bump_version_does_not_clobber_existing_archive(tmp_path):
    """If v3.yaml already exists in history, don't overwrite it."""
    hdir = tmp_path / "prompts.yaml.history"
    hdir.mkdir()
    (hdir / "v3.yaml").write_text(
        yaml.safe_dump({"version": 3, "relevance": {"system": "original_v3"}}),
        encoding="utf-8",
    )
    _write_prompts(tmp_path, {"id": "p", "version": 3, "relevance": {"system": "current_v3"}})

    bump_version(tmp_path, {"relevance": {"system": "v4"}})
    # v3 archive still has the original value, not the current file's value
    archived = yaml.safe_load((hdir / "v3.yaml").read_text(encoding="utf-8"))
    assert archived["relevance"]["system"] == "original_v3"


def test_list_history_returns_versions_ordered(tmp_path):
    hdir = tmp_path / "prompts.yaml.history"
    hdir.mkdir()
    for v in [3, 1, 2]:
        (hdir / f"v{v}.yaml").write_text(
            yaml.safe_dump({"version": v, "relevance": {"system": f"v{v}"}}),
            encoding="utf-8",
        )
    history = list_history(tmp_path)
    assert [h["version"] for h in history] == [1, 2, 3]


def test_list_history_empty_when_dir_missing(tmp_path):
    assert list_history(tmp_path) == []


def test_load_version_returns_archived(tmp_path):
    hdir = tmp_path / "prompts.yaml.history"
    hdir.mkdir()
    (hdir / "v2.yaml").write_text(
        yaml.safe_dump({"version": 2, "relevance": {"system": "yay"}}),
        encoding="utf-8",
    )
    blob = load_version(tmp_path, 2)
    assert blob["relevance"]["system"] == "yay"


def test_load_version_returns_current_when_matches(tmp_path):
    """Requesting the current version returns the live prompts.yaml."""
    _write_prompts(tmp_path, {"id": "p", "version": 7, "relevance": {"system": "cur"}})
    blob = load_version(tmp_path, 7)
    assert blob["relevance"]["system"] == "cur"


def test_load_version_returns_none_for_unknown(tmp_path):
    assert load_version(tmp_path, 99) is None
