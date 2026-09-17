"""Eval harness (DESIGN.md §7) — built first, gates the model before we trust it.

Runs classify+extract against the hand-labeled golden set and reports
per-dimension metrics with bootstrap 95% CIs and per-class sample floors.

    python eval/run_eval.py
    python eval/run_eval.py --limit 20

Acceptance gates (§7.3): F1 >= 0.75 on areas/content_types/severity/entities,
sentiment 3-class accuracy >= 0.70 and sign agreement >= 0.85, primary-area >= 0.80,
windows major >= 0.85. Classes below the sample floor (default 8) are reported
"insufficient sample, not gated".
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:  # Windows consoles default to cp1252; force UTF-8 for emoji output.
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv  # noqa: E402

from pipeline.classify import classify_one  # noqa: E402
from pipeline.config import set_current_product  # noqa: E402
from pipeline.llm import LLMClient  # noqa: E402
from pipeline.product import DEFAULT_PRODUCT, load_product  # noqa: E402
from pipeline.snippets import holdout_subset  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden_set.jsonl"
OUT_DIR = Path(__file__).resolve().parent / "reports"
SAMPLE_FLOOR = 8
BOOTSTRAP_N = 1000

GATES = {
    "areas_f1": 0.75,
    "content_types_f1": 0.75,
    "severity_acc": 0.75,
    "entities_f1": 0.75,
    "sentiment_3class_acc": 0.70,
    "sentiment_sign_agreement": 0.85,
    "primary_area_acc": 0.80,
    "windows_major_acc": 0.85,
}


# --- metric primitives -------------------------------------------------------


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"precision": prec, "recall": rec, "f1": f1}


def _multilabel_counts(pred: set, gold: set) -> tuple[int, int, int]:
    tp = len(pred & gold)
    return tp, len(pred - gold), len(gold - pred)


def _sentiment_class(x: float | None) -> str:
    if x is None:
        return "neutral"
    if x <= -0.2:
        return "neg"
    if x >= 0.2:
        return "pos"
    return "neutral"


def _bootstrap_ci(
    per_item: list[Any], metric_fn: Callable[[list[Any]], float], seed: int = 13
) -> tuple[float, float]:
    """Deterministic bootstrap (fixed-seed LCG, no Math.random dependency)."""
    n = len(per_item)
    if n == 0:
        return (0.0, 0.0)
    state = seed
    samples = []
    for _ in range(BOOTSTRAP_N):
        resample = []
        for _ in range(n):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            resample.append(per_item[state % n])
        samples.append(metric_fn(resample))
    samples.sort()
    lo = samples[int(0.025 * BOOTSTRAP_N)]
    hi = samples[int(0.975 * BOOTSTRAP_N)]
    return (round(lo, 3), round(hi, 3))


# --- evaluation --------------------------------------------------------------


def evaluate(items: list[dict[str, Any]], client: LLMClient) -> dict[str, Any]:
    records = []
    area_pos = Counter()
    ct_pos = Counter()

    for it in items:
        gold = it.get("labels", {})
        try:
            pred, _regex_res, primary = classify_one(it, client)
        except Exception as e:
            records.append({"error": str(e), "id": it.get("id")})
            continue

        for a in gold.get("areas", []):
            area_pos[a] += 1
        for c in gold.get("content_types", []):
            ct_pos[c] += 1

        records.append({
            "id": it.get("id"),
            "areas": _multilabel_counts(set(pred.areas), set(gold.get("areas", []))),
            "content_types": _multilabel_counts(
                set(pred.content_types), set(gold.get("content_types", []))
            ),
            "entities": _entity_counts(pred, gold),
            "sentiment_3class": _sentiment_class(pred.sentiment) == _sentiment_class(gold.get("sentiment")),
            "sentiment_sign": _sign(pred.sentiment) == _sign(gold.get("sentiment")),
            "severity": _severity_match(pred, gold),
            "windows_major": (
                (getattr(getattr(pred, "extras", None), "windows_major", None)
                 == gold.get("windows_major"))
                if gold.get("windows_major") else None
            ),
            "primary_area": (primary == gold.get("primary_area")) if gold.get("primary_area") else None,
        })

    return _summarize(records, area_pos, ct_pos)


def _entity_counts(pred, gold) -> tuple[int, int, int]:
    pred_set = {(e.type, e.product) for e in pred.entities}
    gold_set = {(g.get("type"), g.get("product")) for g in gold.get("entities", [])}
    return _multilabel_counts(pred_set, gold_set)


def _severity_match(pred, gold) -> bool | None:
    if "bug_report" not in gold.get("content_types", []) or not gold.get("severity"):
        return None
    return pred.bug_severity == gold.get("severity")


def _sign(x: float | None) -> int:
    if x is None:
        return 0
    return 1 if x > 0.05 else (-1 if x < -0.05 else 0)


def _summarize(records, area_pos, ct_pos) -> dict[str, Any]:
    valid = [r for r in records if "error" not in r]
    errors = [r for r in records if "error" in r]

    def f1_from(field: str) -> dict[str, Any]:
        triples = [r[field] for r in valid]
        tp = sum(t[0] for t in triples)
        fp = sum(t[1] for t in triples)
        fn = sum(t[2] for t in triples)
        base = _prf(tp, fp, fn)

        def metric(sample):
            stp = sum(t[0] for t in sample)
            sfp = sum(t[1] for t in sample)
            sfn = sum(t[2] for t in sample)
            return _prf(stp, sfp, sfn)["f1"]

        base["f1_ci"] = _bootstrap_ci(triples, metric)
        return base

    def acc_from(field: str) -> dict[str, Any]:
        vals = [r[field] for r in valid if r[field] is not None]
        if not vals:
            return {"accuracy": None, "n": 0, "ci": None}
        acc = sum(1 for v in vals if v) / len(vals)
        ci = _bootstrap_ci(vals, lambda s: sum(1 for v in s if v) / len(s))
        return {"accuracy": round(acc, 3), "n": len(vals), "ci": ci}

    metrics = {
        "areas": f1_from("areas"),
        "content_types": f1_from("content_types"),
        "entities": f1_from("entities"),
        "sentiment_3class": acc_from("sentiment_3class"),
        "sentiment_sign": acc_from("sentiment_sign"),
        "severity": acc_from("severity"),
        "windows_major": acc_from("windows_major"),
        "primary_area": acc_from("primary_area"),
    }

    # Per-class sample floors (§7.1)
    under_floor = {
        "areas": [a for a, n in area_pos.items() if n < SAMPLE_FLOOR],
        "content_types": [c for c, n in ct_pos.items() if n < SAMPLE_FLOOR],
    }

    gates = _check_gates(metrics)
    return {
        "n_items": len(records),
        "n_valid": len(valid),
        "n_errors": len(errors),
        "metrics": metrics,
        "class_counts": {"areas": dict(area_pos), "content_types": dict(ct_pos)},
        "under_sample_floor": under_floor,
        "gates": gates,
        "passed": all(g["pass"] for g in gates.values() if g["gated"]),
    }


def _check_gates(metrics) -> dict[str, Any]:
    def gate(name, value, threshold):
        gated = value is not None
        return {"value": value, "threshold": threshold, "gated": gated,
                "pass": (value is not None and value >= threshold)}

    return {
        "areas_f1": gate("areas_f1", metrics["areas"]["f1"], GATES["areas_f1"]),
        "content_types_f1": gate("content_types_f1", metrics["content_types"]["f1"], GATES["content_types_f1"]),
        "entities_f1": gate("entities_f1", metrics["entities"]["f1"], GATES["entities_f1"]),
        "severity_acc": gate("severity_acc", metrics["severity"]["accuracy"], GATES["severity_acc"]),
        "sentiment_3class_acc": gate("sentiment_3class_acc", metrics["sentiment_3class"]["accuracy"], GATES["sentiment_3class_acc"]),
        "sentiment_sign_agreement": gate("sentiment_sign_agreement", metrics["sentiment_sign"]["accuracy"], GATES["sentiment_sign_agreement"]),
        "primary_area_acc": gate("primary_area_acc", metrics["primary_area"]["accuracy"], GATES["primary_area_acc"]),
        "windows_major_acc": gate("windows_major_acc", metrics["windows_major"]["accuracy"], GATES["windows_major_acc"]),
    }


# --- markdown report ---------------------------------------------------------


def to_markdown(summary: dict[str, Any]) -> str:
    lines = ["# Eval report", ""]
    lines.append(f"- items: {summary['n_items']} (valid {summary['n_valid']}, errors {summary['n_errors']})")
    lines.append(f"- **overall pass: {summary['passed']}**")
    lines.append("")
    lines.append("| gate | value | threshold | pass | gated |")
    lines.append("|---|---|---|---|---|")
    for name, g in summary["gates"].items():
        v = "—" if g["value"] is None else f"{g['value']:.3f}"
        lines.append(f"| {name} | {v} | {g['threshold']} | {'✅' if g['pass'] else '❌'} | {g['gated']} |")
    lines.append("")
    if any(summary["under_sample_floor"].values()):
        lines.append("## Under sample floor (not gated)")
        for dim, classes in summary["under_sample_floor"].items():
            if classes:
                lines.append(f"- {dim}: {', '.join(classes)}")
    return "\n".join(lines)


def _load_eval_items(product_id: str) -> list[dict[str, Any]]:
    """Preferred: holdout-flagged snippets from products/<id>/examples/.
    Fallback: legacy eval/golden_set.jsonl."""
    product = load_product(product_id)
    set_current_product(product)
    held = holdout_subset(product.snippets)
    if held:
        return [s.to_classify_item() for s in held]
    if GOLDEN.exists():
        return [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    return []


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", default=DEFAULT_PRODUCT, help="Product id under products/.")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)

    items = _load_eval_items(args.product)
    if args.limit:
        items = items[: args.limit]
    if not items:
        print(
            f"[eval] no eval items for product {args.product!r}. "
            f"Add holdout-flagged snippets under products/{args.product}/examples/, "
            f"or populate eval/golden_set.jsonl (legacy)."
        )
        return 2

    client = LLMClient("classify")
    if not client.health_check():
        print("[eval] LLM not reachable; cannot run eval.")
        return 3

    summary = evaluate(items, client)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "latest.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    md = to_markdown(summary)
    (OUT_DIR / "latest.md").write_text(md, encoding="utf-8")
    print(md)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
