"""Per-stage snapshots to temp files for observability.

After each pipeline stage, we capture the joined "everything about this item"
view of the warehouse as JSONL, plus a small meta file with counters/durations.
Snapshots land in:

    data/<product_id>/temp_runs/<run_id>/
        manifest.json          run/product/week metadata
        stages.json            index of captured stages (order preserved)
        <stage>.jsonl          one row per item, joined view
        <stage>.meta.json      counters + duration + errors for that stage
        config_snapshot/       full config as of run start (see snapshot_config)

This is a side-channel for the webui to answer "show me what the data looked
like after stage X" and "what settings produced this run". The warehouse is
still the authoritative store; nothing here is read back by the pipeline.

Fetch has no warehouse rows yet (normalize populates items), so its snapshot
is a small summary of the raw JSONL that landed on disk.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pipeline import storage
from pipeline.config import CONFIG_DIR, app_config, resolve_path
from pipeline.product import PRODUCTS_DIR


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def temp_run_root(product_id: str) -> Path:
    return resolve_path(app_config()["paths"]["data_root"]) / product_id / "temp_runs"


def temp_run_dir(product_id: str, run_id: str) -> Path:
    return temp_run_root(product_id) / run_id


def init_run(
    product_id: str,
    run_id: str,
    week_id: str,
    versions: dict[str, str],
    window: dict[str, Any],
    runtime_context: Optional[dict[str, Any]] = None,
) -> Path:
    """Create the temp_runs/<run_id>/ dir, write manifest.json, and snapshot
    the config (global app.yaml + all product YAMLs + CLI/runtime context).

    Returns the path.
    """
    d = temp_run_dir(product_id, run_id)
    d.mkdir(parents=True, exist_ok=True)
    manifest = {
        "product_id": product_id,
        "run_id": run_id,
        "week_id": week_id,
        "started_at": _now_iso(),
        "versions": versions,
        "time_window": {
            "mode": window.get("mode"),
            "since_ts": window.get("since_ts"),
            "until_ts": window.get("until_ts"),
        },
    }
    (d / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (d / "stages.json").write_text(json.dumps({"stages": []}, indent=2), encoding="utf-8")
    snapshot_config(product_id, run_id, week_id, versions, window, runtime_context or {})
    return d


def snapshot_config(
    product_id: str,
    run_id: str,
    week_id: str,
    versions: dict[str, str],
    window: dict[str, Any],
    runtime_context: dict[str, Any],
) -> Path:
    """Copy the config files that determined this run into
    temp_runs/<run_id>/config_snapshot/. Preserves the exact byte contents so
    "what settings produced this report" is answerable even after config edits.

    Layout:
        config_snapshot/
            app.yaml               (config/app.yaml at run start)
            product/*.yaml         (all YAMLs under products/<product_id>/)
            runtime.json           run_id/week_id/versions/window/CLI context

    Copy-on-failure is best-effort — if a file is missing (fresh product
    with no llm_routing.yaml, say) we skip it and note it in runtime.json.
    """
    d = temp_run_dir(product_id, run_id) / "config_snapshot"
    (d / "product").mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    missing: list[str] = []

    # Global app.yaml
    src = CONFIG_DIR / "app.yaml"
    if src.exists():
        shutil.copy2(src, d / "app.yaml")
        copied.append("app.yaml")
    else:
        missing.append("app.yaml")

    # Product YAMLs
    product_dir = PRODUCTS_DIR / product_id
    if product_dir.is_dir():
        for yaml_file in sorted(product_dir.glob("*.yaml")):
            shutil.copy2(yaml_file, d / "product" / yaml_file.name)
            copied.append(f"product/{yaml_file.name}")

    # Runtime context — CLI args, computed window, versions, timings
    runtime = {
        "product_id": product_id,
        "run_id": run_id,
        "week_id": week_id,
        "captured_at": _now_iso(),
        "versions": versions,
        "time_window": window,
        "runtime_context": runtime_context,
        "copied_files": copied,
        "missing_files": missing,
    }
    (d / "runtime.json").write_text(json.dumps(runtime, indent=2, default=str), encoding="utf-8")
    return d


def _append_stage_index(run_dir: Path, stage: str) -> None:
    idx_path = run_dir / "stages.json"
    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        idx = {"stages": []}
    if stage not in idx["stages"]:
        idx["stages"].append(stage)
    idx_path.write_text(json.dumps(idx, indent=2), encoding="utf-8")


def _joined_items(week_id: str) -> list[dict[str, Any]]:
    """Full 'everything about this item' view. LEFT JOINs the 1:1 tables and
    stitches in the many:1 tables in Python (areas, entities, group memberships).

    Returns an empty list if the base items table is empty for this week (e.g.
    normalize hasn't run yet)."""
    base = storage.query(
        """
        SELECT
            i.*,
            ic.content_types_json,
            ic.sentiment,
            ic.summary,
            ic.confidence      AS classification_confidence,
            ic.primary_area,
            ic.model           AS classification_model,
            ic.classified_at,
            ba.severity,
            ba.is_regression,
            ba.reproducibility,
            ba.repro_steps_quality,
            ba.repro_steps_json,
            ba.preconditions_json,
            ra.specificity,
            ra.existing_workaround_mentioned,
            ictx.user_context,
            ictx.windows_version_major,
            ictx.windows_version_feature_update,
            ictx.windows_version_build,
            ictx.windows_version_channel,
            ictx.windows_version_confidence,
            re.kb_numbers,
            re.cve_ids,
            re.build_numbers,
            s.score,
            s.engagement_w,
            s.source_w,
            s.recency_w,
            s.computed_at      AS score_computed_at
        FROM items i
        LEFT JOIN item_classifications ic  ON ic.item_id  = i.id
        LEFT JOIN bug_attributes       ba  ON ba.item_id  = i.id
        LEFT JOIN request_attributes   ra  ON ra.item_id  = i.id
        LEFT JOIN item_context         ictx ON ictx.item_id = i.id
        LEFT JOIN regex_extractions    re  ON re.item_id  = i.id
        LEFT JOIN scores               s   ON s.item_id   = i.id
        WHERE i.week_id = ?
        ORDER BY i.created_at DESC
        """,
        [week_id],
    )
    if not base:
        return []

    ids = [r["id"] for r in base]
    placeholders = ",".join("?" * len(ids))

    areas = storage.query(
        f"SELECT item_id, area, is_primary FROM item_areas WHERE item_id IN ({placeholders})",
        ids,
    )
    areas_by_item: dict[str, list[dict[str, Any]]] = {}
    for a in areas:
        areas_by_item.setdefault(a["item_id"], []).append(
            {"area": a["area"], "is_primary": a["is_primary"]}
        )

    ents = storage.query(
        f"""SELECT item_id, type, product_key, role, product, version,
                   confidence, verbatim
             FROM entity_mentions WHERE item_id IN ({placeholders})""",
        ids,
    )
    ents_by_item: dict[str, list[dict[str, Any]]] = {}
    for e in ents:
        ents_by_item.setdefault(e["item_id"], []).append(
            {k: e[k] for k in e.keys() if k != "item_id"}
        )

    groups = storage.query(
        f"""SELECT item_id, week_id, area, group_key, is_canonical
             FROM week_group_members WHERE item_id IN ({placeholders})""",
        ids,
    )
    groups_by_item: dict[str, list[dict[str, Any]]] = {}
    for g in groups:
        groups_by_item.setdefault(g["item_id"], []).append(
            {"area": g["area"], "group_key": g["group_key"], "is_canonical": g["is_canonical"]}
        )

    for row in base:
        iid = row["id"]
        row["areas"] = areas_by_item.get(iid, [])
        row["entities"] = ents_by_item.get(iid, [])
        row["group_membership"] = groups_by_item.get(iid, [])
    return base


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


def snapshot_stage(
    product_id: str,
    run_id: str,
    week_id: str,
    stage: str,
    stage_result: dict[str, Any],
    duration_s: float,
) -> Optional[Path]:
    """Write <stage>.jsonl (joined item view) + <stage>.meta.json.

    Fetch is a special case: no warehouse rows yet, so the .jsonl contains one
    row per raw JSONL file that landed on disk, with counts.

    Returns the .jsonl path, or None if capture couldn't run (e.g. warehouse
    hasn't been created yet — early failure)."""
    run_dir = temp_run_dir(product_id, run_id)
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None

    meta = {
        "stage": stage,
        "captured_at": _now_iso(),
        "duration_s": duration_s,
        "week_id": week_id,
        "counters": stage_result.get("counters", {}) if isinstance(stage_result, dict) else {},
        "summary": {k: v for k, v in (stage_result or {}).items() if k != "counters"},
    }

    jsonl_path = run_dir / f"{stage}.jsonl"
    meta_path = run_dir / f"{stage}.meta.json"

    try:
        if stage == "fetch":
            rows = _fetch_summary_rows(product_id, week_id)
            # For fetch, one row per source *file*. The semantic "how many
            # items were fetched" is the sum of line_count across files —
            # that keeps this counter comparable with downstream stages
            # (which count items, not files).
            meta["row_count"] = sum(int(r.get("line_count") or 0) for r in rows)
            meta["file_count"] = len(rows)
        else:
            rows = _joined_items(week_id)
            meta["row_count"] = len(rows)
        _write_jsonl(jsonl_path, rows)
    except Exception as e:
        meta["capture_error"] = str(e)
        meta["row_count"] = 0
        # Still write an empty jsonl so the UI can render "0 rows"
        _write_jsonl(jsonl_path, [])

    meta_path.write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    _append_stage_index(run_dir, stage)
    return jsonl_path


def _fetch_summary_rows(product_id: str, week_id: str) -> list[dict[str, Any]]:
    """Fetch stage summary: one row per raw JSONL file for this week."""
    raw_root = resolve_path(app_config()["paths"]["data_root"]) / product_id / "raw"
    if not raw_root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for wd in raw_root.glob(f"*/{week_id}"):
        source = wd.parent.name
        for f in sorted(wd.glob("*.jsonl")):
            try:
                line_count = sum(1 for _ in f.open("r", encoding="utf-8"))
                size = f.stat().st_size
            except Exception:
                line_count, size = 0, 0
            rows.append({
                "source": source,
                "path": str(f),
                "file_name": f.name,
                "line_count": line_count,
                "bytes": size,
            })
    return rows
