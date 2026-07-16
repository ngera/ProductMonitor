"""Tests for pipeline/features.py (ADR-0006).

Feature flags default off. Product overrides win over global. Cache clears
correctly.
"""

from __future__ import annotations

import yaml
from pathlib import Path

from pipeline import features


def test_unknown_flag_defaults_false(clear_feature_cache):
    assert features.enabled("does_not_exist_flag") is False


def test_known_flag_reads_global(clear_feature_cache):
    # trust_plugins_dir is defined in config/features.yaml at False
    assert features.enabled("trust_plugins_dir") is False


def test_all_flags_returns_all_defined(clear_feature_cache):
    flags = features.all_flags()
    # config/features.yaml defines at least the Phase 1-5 flags
    assert "trust_plugins_dir" in flags
    assert "wizard_enabled" in flags
    assert "evals_enabled" in flags
    assert isinstance(flags["trust_plugins_dir"], bool)


def test_product_override_wins(tmp_path: Path, monkeypatch, clear_feature_cache):
    """Product-level override in products/<pid>/features.yaml beats global."""
    # Set up an isolated products dir
    products_dir = tmp_path / "products"
    products_dir.mkdir()
    product_dir = products_dir / "test-product"
    product_dir.mkdir()
    (product_dir / "features.yaml").write_text(
        yaml.safe_dump({"features": {"trust_plugins_dir": True}}),
        encoding="utf-8",
    )
    # Point PRODUCTS_DIR at our temp dir
    monkeypatch.setattr(features, "PRODUCTS_DIR", products_dir)
    features.clear_cache()

    assert features.enabled("trust_plugins_dir", product_id="test-product") is True
    # Without product id → still reads global (False)
    assert features.enabled("trust_plugins_dir") is False


def test_missing_product_features_file_is_fine(clear_feature_cache):
    """A product without features.yaml gets global defaults, no crash."""
    assert features.enabled("trust_plugins_dir", product_id="nonexistent") is False


def test_clear_cache_reloads(tmp_path: Path, monkeypatch, clear_feature_cache):
    """After editing config/features.yaml, clear_cache() picks up the change."""
    fake_config = tmp_path / "features.yaml"
    fake_config.write_text(
        yaml.safe_dump({"features": {"custom_flag": True}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(features, "_FEATURES_YAML", fake_config)
    features.clear_cache()

    assert features.enabled("custom_flag") is True

    # Update on disk
    fake_config.write_text(
        yaml.safe_dump({"features": {"custom_flag": False}}),
        encoding="utf-8",
    )
    # Without clear_cache, cached value stays
    assert features.enabled("custom_flag") is True
    # After clear_cache, fresh read
    features.clear_cache()
    assert features.enabled("custom_flag") is False
