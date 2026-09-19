"""Cancel in-flight UI runs from the product Summary banner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("structlog")

from fastapi.testclient import TestClient

from pipeline import product as product_mod


@pytest.fixture
def isolated_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    products = tmp_path / "products"
    data = tmp_path / "data"
    products.mkdir()
    data.mkdir()

    monkeypatch.setattr("pipeline.product.PRODUCTS_DIR", products)
    monkeypatch.setattr("pipeline.features.PRODUCTS_DIR", products)
    monkeypatch.setattr("webui.app.PRODUCTS_DIR", products)
    product_mod.clear_cache()
    from pipeline import features as _f
    _f.clear_cache()

    from pipeline import config as _c
    orig = _c.app_config

    def _patched():
        cfg = dict(orig())
        paths = dict(cfg.get("paths") or {})
        paths["data_root"] = str(data)
        cfg["paths"] = paths
        return cfg

    # Bound imports keep their own reference — patch each consumer.
    monkeypatch.setattr(_c, "app_config", _patched)
    from webui import app as _wa
    from webui.services import runs as _runs
    monkeypatch.setattr(_wa, "app_config", _patched)
    monkeypatch.setattr(_runs, "app_config", _patched)

    return products, data


def _mark_running(data: Path, product_id: str, run_id: str) -> Path:
    logs = data / product_id / "run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    marker = logs / f"{run_id}.running"
    marker.write_text("in flight\n", encoding="utf-8")
    return marker


def test_cancel_clears_marker_and_writes_cancelled_json(isolated_layout):
    products, data = isolated_layout
    product_mod.scaffold_product("acme", "Acme")
    run_id = "ui-20260919T120000-abc123"
    marker = _mark_running(data, "acme", run_id)

    from webui.app import app, _cancel_pipeline_run
    ok, msg = _cancel_pipeline_run("acme", run_id)
    assert ok
    assert "cleared" in msg or "cancelled" in msg
    assert not marker.exists()

    payload = json.loads(
        (data / "acme" / "run_logs" / f"{run_id}.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "cancelled"
    assert payload["run_id"] == run_id


def test_cancel_route_redirects_to_summary(isolated_layout):
    products, data = isolated_layout
    product_mod.scaffold_product("acme", "Acme")
    run_id = "ui-20260919T120000-def456"
    _mark_running(data, "acme", run_id)

    from webui.app import app
    with TestClient(app) as client:
        resp = client.post(
            f"/products/acme/runs/{run_id}/cancel",
            data={"next": "/products/acme"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/products/acme?")
    assert "notice=" in loc
    assert not (data / "acme" / "run_logs" / f"{run_id}.running").exists()


def test_summary_shows_cancel_button_for_live_run(isolated_layout):
    products, data = isolated_layout
    product_mod.scaffold_product("acme", "Acme")
    run_id = "ui-20260919T120000-ghi789"
    _mark_running(data, "acme", run_id)

    from webui.app import app
    with TestClient(app) as client:
        resp = client.get("/products/acme")
    assert resp.status_code == 200
    assert "Run in progress" in resp.text
    assert "Cancel run" in resp.text
    assert f'/products/acme/runs/{run_id}/cancel' in resp.text


def test_run_detail_shows_cancel_button_when_running(isolated_layout):
    products, data = isolated_layout
    product_mod.scaffold_product("acme", "Acme")
    run_id = "ui-20260919T120000-jkl012"
    _mark_running(data, "acme", run_id)

    from webui.app import app
    with TestClient(app) as client:
        resp = client.get(f"/products/acme/runs/{run_id}")
    assert resp.status_code == 200
    assert "running…" in resp.text
    assert "Cancel run" in resp.text
    assert f'/products/acme/runs/{run_id}/cancel' in resp.text
    assert f'value="/products/acme/runs/{run_id}"' in resp.text


def test_run_detail_hides_cancel_when_finished(isolated_layout):
    products, data = isolated_layout
    product_mod.scaffold_product("acme", "Acme")
    run_id = "ui-20260919T120000-mno345"
    logs = data / "acme" / "run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "status": "success", "week_id": "2026-W38"}),
        encoding="utf-8",
    )

    from webui.app import app
    with TestClient(app) as client:
        resp = client.get(f"/products/acme/runs/{run_id}")
    assert resp.status_code == 200
    assert "Cancel run" not in resp.text


def test_runs_list_shows_cancel_for_running_row(isolated_layout):
    products, data = isolated_layout
    product_mod.scaffold_product("acme", "Acme")
    run_id = "ui-20260919T120000-pqr678"
    _mark_running(data, "acme", run_id)

    from webui.app import app
    with TestClient(app) as client:
        resp = client.get("/products/acme/runs")
    assert resp.status_code == 200
    assert "running" in resp.text
    assert f'/products/acme/runs/{run_id}/cancel' in resp.text
    assert 'value="/products/acme/runs"' in resp.text
    assert "cancel" in resp.text
    assert "link-action" in resp.text


def test_resume_route_spawns_from_stage(isolated_layout, monkeypatch):
    products, data = isolated_layout
    product_mod.scaffold_product("acme", "Acme")
    run_id = "ui-20260919T120000-abc123"
    logs = data / "acme" / "run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"{run_id}.json").write_text(
        json.dumps({
            "run_id": run_id,
            "product_id": "acme",
            "week_id": "2026-W38",
            "status": "cancelled",
            "stage_durations": {
                "fetch": 1.0, "normalize": 0.5, "filter": 0.2, "relevance": 10.0,
            },
            "errors": ["Cancelled by operator"],
        }),
        encoding="utf-8",
    )
    (logs / f"{run_id}.out").write_text(
        "\n".join([
            "event=stage_start stage=fetch",
            "event=stage_done stage=fetch seconds=1.0",
            "event=stage_start stage=normalize",
            "event=stage_done stage=normalize seconds=0.5",
            "event=stage_start stage=filter",
            "event=stage_done stage=filter seconds=0.2",
            "event=stage_start stage=relevance",
            "event=stage_done stage=relevance seconds=10.0",
            "event=stage_start stage=classify",
        ]) + "\n",
        encoding="utf-8",
    )

    spawned: dict = {}

    def _fake_spawn(product_id, **kwargs):
        spawned.update(kwargs)
        spawned["product_id"] = product_id
        return "ui-20260919T130000-resume"

    monkeypatch.setattr("webui.app._spawn_pipeline_run", _fake_spawn)

    from webui.app import app
    with TestClient(app) as client:
        r = client.post(f"/products/acme/runs/{run_id}/resume", follow_redirects=False)
    assert r.status_code == 303
    assert "/products/acme/runs/ui-20260919T130000-resume" in r.headers["location"]
    assert spawned["from_stage"] == "classify"
    assert spawned["skip_fetch"] is True
    assert spawned["week_id"] == "2026-W38"
    assert spawned["resumed_from"] == run_id


def test_run_detail_shows_resume_when_cancelled(isolated_layout):
    products, data = isolated_layout
    product_mod.scaffold_product("acme", "Acme")
    run_id = "ui-20260919T120000-abc123"
    logs = data / "acme" / "run_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"{run_id}.json").write_text(
        json.dumps({
            "run_id": run_id,
            "status": "cancelled",
            "week_id": "2026-W38",
            "stage_durations": {"fetch": 1.0, "relevance": 2.0},
            "errors": [],
        }),
        encoding="utf-8",
    )
    (logs / f"{run_id}.out").write_text(
        "event=stage_start stage=classify\n",
        encoding="utf-8",
    )

    from webui.app import app
    with TestClient(app) as client:
        r = client.get(f"/products/acme/runs/{run_id}")
    assert r.status_code == 200
    assert "Resume from classify" in r.text
    assert f"/products/acme/runs/{run_id}/resume" in r.text
