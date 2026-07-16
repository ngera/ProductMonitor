"""End-to-end evals (POST_V1_PLAN §4.10, ADR-0009).

Scores classify output against a time-based golden set of snippets.
Runs as a pipeline stage after `classify` (before `render`), or as a
dry run to answer "how would eval change if we swap prompt X for Y?"
(used by §4.5 prompt suggestions).

Design (locked-in decisions):

- **End-to-end only** (D14). No per-stage labels required — we score
  classify's output against snippet.labels directly.
- **Time-based golden split** (D8, D15). Snippets authored on/before
  `products/<pid>/product.yaml -> eval.golden_set_cutoff_date` are
  scored; snippets after are training few-shots. See snippets.py.
- **Min sample size 30** (D16). Below the floor we return a
  "insufficient_data" summary — no misleading point estimates.
- **95% bootstrap CIs on every metric** (D17). Deterministic — same
  input, same seed → same intervals.
- **Regression** (D18). Metric drop > 5pp AND the CI on the delta
  excludes zero, vs rolling median of last 3 runs.
- **Machine-readable output** at
  `data/<pid>/temp_runs/<run_id>/eval_summary.json` — future CI hook.

Off by default. Guarded by `features.evals_enabled` (product-scoped).
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import structlog

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_GOLDEN_SET_SIZE = 30
BOOTSTRAP_ITERATIONS = 1000
REGRESSION_DROP_PP = 5.0            # metric drop > 5 percentage points
DEFAULT_CUTOFF_MONTHS = 6           # snippets older than this land in golden

_INSUFFICIENT_DATA = "insufficient_data"


# ---------------------------------------------------------------------------
# Metric primitives — pure functions, no I/O
# ---------------------------------------------------------------------------


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    """Precision / recall / F1 from a TP/FP/FN triple."""
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4)}


def _multilabel_counts(pred: set, gold: set) -> tuple[int, int, int]:
    """Return (TP, FP, FN) for a multi-label prediction against gold."""
    return len(pred & gold), len(pred - gold), len(gold - pred)


def _bootstrap_ci(
    per_item: list[Any],
    metric_fn: Callable[[list[Any]], float],
    *,
    seed: int = 13,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> tuple[float, float]:
    """Deterministic 95% bootstrap CI.

    Uses a fixed-seed LCG (not `random`) so the output is byte-stable
    across Python versions — regression tests can pin values.
    """
    n = len(per_item)
    if n == 0:
        return (0.0, 0.0)
    state = seed
    samples = []
    for _ in range(iterations):
        resample = []
        for _ in range(n):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            resample.append(per_item[state % n])
        samples.append(metric_fn(resample))
    samples.sort()
    lo = samples[int(0.025 * iterations)]
    hi = samples[int(0.975 * iterations)]
    return (round(lo, 4), round(hi, 4))


def _sentiment_class(x: Optional[float]) -> str:
    if x is None:
        return "neutral"
    if x <= -0.2:
        return "neg"
    if x >= 0.2:
        return "pos"
    return "neutral"


def _sign(x: Optional[float]) -> int:
    if x is None:
        return 0
    return 1 if x > 0.05 else (-1 if x < -0.05 else 0)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class EvalPerItem:
    """Per-item scoring — kept for bootstrap resampling + failure drilldowns."""

    id: str
    areas: tuple[int, int, int]                    # (tp, fp, fn)
    content_types: tuple[int, int, int]
    entities: tuple[int, int, int]
    kb: tuple[int, int, int]
    primary_area_correct: Optional[bool]
    sentiment_3class_correct: Optional[bool]
    sentiment_sign_correct: Optional[bool]
    severity_correct: Optional[bool]
    error: str = ""


@dataclass
class EvalSummary:
    """Machine-readable eval output. Written to eval_summary.json + rendered
    into the run detail scorecard."""

    status: str                     # "ok" | "insufficient_data" | "no_snippets"
    product_id: str
    run_id: str
    n_snippets_total: int           # all snippets loaded for the product
    n_golden: int                   # after time-based split
    n_evaluated: int                # golden set that produced valid predictions
    n_errors: int
    cutoff_date: Optional[str]      # ISO
    metrics: dict[str, dict[str, Any]] = field(default_factory=dict)
    regressions: list[str] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "product_id": self.product_id,
            "run_id": self.run_id,
            "n_snippets_total": self.n_snippets_total,
            "n_golden": self.n_golden,
            "n_evaluated": self.n_evaluated,
            "n_errors": self.n_errors,
            "cutoff_date": self.cutoff_date,
            "metrics": self.metrics,
            "regressions": self.regressions,
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_summary(
    per_item: list[EvalPerItem],
    *,
    product_id: str,
    run_id: str,
    n_snippets_total: int,
    n_golden: int,
    cutoff_date: Optional[datetime],
    generated_at: Optional[datetime] = None,
) -> EvalSummary:
    """Aggregate per-item scores into an EvalSummary with bootstrap CIs.

    This is the pure aggregation step — no LLM calls, no filesystem I/O.
    Callers do the classification loop, populate per-item, then call this.
    """
    valid = [r for r in per_item if not r.error]
    errors = [r for r in per_item if r.error]
    generated_at = generated_at or datetime.now(timezone.utc)

    if n_golden < MIN_GOLDEN_SET_SIZE:
        return EvalSummary(
            status=_INSUFFICIENT_DATA,
            product_id=product_id,
            run_id=run_id,
            n_snippets_total=n_snippets_total,
            n_golden=n_golden,
            n_evaluated=len(valid),
            n_errors=len(errors),
            cutoff_date=cutoff_date.isoformat() if cutoff_date else None,
            generated_at=generated_at.isoformat(),
        )

    metrics: dict[str, dict[str, Any]] = {}
    metrics["primary_area"] = _accuracy_metric(valid, "primary_area_correct")
    metrics["areas"] = _f1_metric(valid, "areas")
    metrics["content_types"] = _f1_metric(valid, "content_types")
    metrics["entities"] = _f1_metric(valid, "entities")
    metrics["kb"] = _f1_metric(valid, "kb")
    metrics["sentiment_3class"] = _accuracy_metric(valid, "sentiment_3class_correct")
    metrics["sentiment_sign"] = _accuracy_metric(valid, "sentiment_sign_correct")
    metrics["severity"] = _accuracy_metric(valid, "severity_correct")

    return EvalSummary(
        status="ok",
        product_id=product_id,
        run_id=run_id,
        n_snippets_total=n_snippets_total,
        n_golden=n_golden,
        n_evaluated=len(valid),
        n_errors=len(errors),
        cutoff_date=cutoff_date.isoformat() if cutoff_date else None,
        metrics=metrics,
        generated_at=generated_at.isoformat(),
    )


def _f1_metric(valid: list[EvalPerItem], field_name: str) -> dict[str, Any]:
    """Aggregate a multi-label field into F1 + bootstrap CI."""
    triples = [getattr(r, field_name) for r in valid]
    tp = sum(t[0] for t in triples)
    fp = sum(t[1] for t in triples)
    fn = sum(t[2] for t in triples)
    prf = _prf(tp, fp, fn)

    def _f1_of(sample: list[tuple[int, int, int]]) -> float:
        stp = sum(t[0] for t in sample)
        sfp = sum(t[1] for t in sample)
        sfn = sum(t[2] for t in sample)
        return _prf(stp, sfp, sfn)["f1"]

    prf["f1_ci"] = list(_bootstrap_ci(triples, _f1_of))
    prf["n"] = len(triples)
    return prf


def _accuracy_metric(valid: list[EvalPerItem], field_name: str) -> dict[str, Any]:
    """Aggregate a per-item boolean into accuracy + bootstrap CI.

    Skips items where the field is None (label wasn't present in gold)."""
    vals = [getattr(r, field_name) for r in valid if getattr(r, field_name) is not None]
    if not vals:
        return {"accuracy": None, "n": 0, "ci": None}
    acc = round(sum(1 for v in vals if v) / len(vals), 4)
    ci = list(_bootstrap_ci(vals, lambda s: sum(1 for v in s if v) / len(s)))
    return {"accuracy": acc, "n": len(vals), "ci": ci}


# ---------------------------------------------------------------------------
# Scoring helpers — turn a Snippet + classify output into an EvalPerItem
# ---------------------------------------------------------------------------


def score_prediction(
    snippet_id: str,
    predicted: Any,
    regex_res: Any,
    primary_area: str,
    gold_labels: dict[str, Any],
) -> EvalPerItem:
    """Score ONE prediction against gold. Pure function — no I/O.

    `predicted` is a normalized Classification (from classify.classify_one),
    `regex_res` is a RegexExtractions instance, `primary_area` is the
    primary-area string that choose_primary_area returned.
    """
    areas_triple = _multilabel_counts(
        set(getattr(predicted, "areas", []) or []),
        set(gold_labels.get("areas") or []),
    )
    ct_triple = _multilabel_counts(
        set(getattr(predicted, "content_types", []) or []),
        set(gold_labels.get("content_types") or []),
    )
    entities_triple = _multilabel_counts(
        {(e.type, e.vendor, e.product) for e in (getattr(predicted, "entities", []) or [])},
        {(g.get("type"), g.get("vendor"), g.get("product"))
         for g in (gold_labels.get("entities") or [])},
    )
    kb_triple = _multilabel_counts(
        {k.upper() for k in (getattr(regex_res, "kb_numbers", []) or [])},
        {k.upper() for k in (gold_labels.get("kb_numbers") or [])},
    )
    primary_correct = (
        (primary_area == gold_labels["primary_area"])
        if gold_labels.get("primary_area") else None
    )
    sent_class_correct = (
        _sentiment_class(getattr(predicted, "sentiment", None))
        == _sentiment_class(gold_labels.get("sentiment"))
    ) if gold_labels.get("sentiment") is not None else None
    sent_sign_correct = (
        _sign(getattr(predicted, "sentiment", None)) == _sign(gold_labels.get("sentiment"))
    ) if gold_labels.get("sentiment") is not None else None
    severity_correct: Optional[bool] = None
    if "bug_report" in (gold_labels.get("content_types") or []) and gold_labels.get("severity"):
        severity_correct = (
            getattr(predicted, "bug_severity", None) == gold_labels.get("severity")
        )
    return EvalPerItem(
        id=snippet_id,
        areas=areas_triple,
        content_types=ct_triple,
        entities=entities_triple,
        kb=kb_triple,
        primary_area_correct=primary_correct,
        sentiment_3class_correct=sent_class_correct,
        sentiment_sign_correct=sent_sign_correct,
        severity_correct=severity_correct,
    )


# ---------------------------------------------------------------------------
# Cutoff-date resolution + snippet split
# ---------------------------------------------------------------------------


def resolve_cutoff_date(product_meta: dict[str, Any]) -> Optional[datetime]:
    """Pick the eval cutoff for a product.

    Order of precedence:
      1. Explicit `eval.golden_set_cutoff_date` in product.yaml (ISO string)
      2. Default: 6 months ago (DEFAULT_CUTOFF_MONTHS)
    """
    eval_cfg = (product_meta or {}).get("eval") or {}
    raw = eval_cfg.get("golden_set_cutoff_date")
    if raw:
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            log.warning("eval_cutoff_parse_failed", raw=raw)

    now = datetime.now(timezone.utc)
    # Rough "6 months ago" — 30-day months are close enough for a rolling cutoff.
    return now.replace(day=1) - _months_delta(DEFAULT_CUTOFF_MONTHS)


def _months_delta(months: int):
    """Approx delta of N months as a timedelta (30-day months). Used only
    for computing a rolling cutoff — not calendar-precise on purpose."""
    from datetime import timedelta
    return timedelta(days=30 * months)


# ---------------------------------------------------------------------------
# Regression detection (D18)
# ---------------------------------------------------------------------------


def detect_regressions(
    current: dict[str, dict[str, Any]],
    history: list[dict[str, dict[str, Any]]],
) -> list[str]:
    """Compare `current` metrics vs a rolling median baseline.

    Flags a metric as regressed when BOTH:
      1. Point estimate drops > 5pp vs the median of the last 3 runs.
      2. The current CI upper bound falls below the median-of-baselines,
         which corresponds to "the CI on the delta excludes zero" for the
         simple bootstrap we ship.

    Returns a list of metric names that regressed. Empty when there's
    nothing to compare (< 1 history entry) or no metric moved enough.
    """
    if not history:
        return []
    baseline = history[-3:]         # last 3 runs
    regressed: list[str] = []
    for name, m in current.items():
        cur = _point_estimate(m)
        cur_ci = _ci_bounds(m)
        if cur is None:
            continue
        baseline_vals = [
            _point_estimate(h.get(name, {})) for h in baseline
            if h.get(name) and _point_estimate(h[name]) is not None
        ]
        if not baseline_vals:
            continue
        med = statistics.median(baseline_vals)
        drop_pp = (med - cur) * 100.0
        if drop_pp <= REGRESSION_DROP_PP:
            continue
        # CI check: upper bound of current < baseline median
        if cur_ci is not None and cur_ci[1] < med:
            regressed.append(name)
        elif cur_ci is None:
            regressed.append(name)   # no CI info; fall back to point-only
    return regressed


def _point_estimate(metric: dict[str, Any]) -> Optional[float]:
    """A metric may carry either 'f1' (multi-label) or 'accuracy' (per-item)."""
    if metric.get("f1") is not None:
        return float(metric["f1"])
    if metric.get("accuracy") is not None:
        return float(metric["accuracy"])
    return None


def _ci_bounds(metric: dict[str, Any]) -> Optional[tuple[float, float]]:
    ci = metric.get("f1_ci") or metric.get("ci")
    if ci and len(ci) == 2:
        return (float(ci[0]), float(ci[1]))
    return None


# ---------------------------------------------------------------------------
# Pipeline stage — the actual "score the golden set" runner
# ---------------------------------------------------------------------------


def _summary_path(product_id: str, run_id: str) -> Path:
    """Machine-readable eval summary path."""
    from pipeline.config import app_config, resolve_path
    data_root = resolve_path(app_config()["paths"]["data_root"])
    return data_root / product_id / "temp_runs" / run_id / "eval_summary.json"


def run_eval(run_id: str, week_id: Optional[str] = None) -> dict[str, Any]:
    """Pipeline-stage entry point. Classifies the golden set, aggregates,
    writes the summary JSON to disk, returns a counters/summary dict.

    Feature-flagged. Callers should check `features.enabled("evals_enabled",
    product_id)` first — but we short-circuit here as a safety net too.

    Returns `{counters, summary}` on success; `{counters, summary: None}`
    when the flag is off or no snippets exist.
    """
    _ = week_id  # eval is per-run, not per-week; left in signature for orchestrator symmetry.

    from pipeline import features as _feat
    from pipeline.config import current_product
    from pipeline.snippets import golden_set_subset

    product = current_product()

    if not _feat.enabled("evals_enabled", product.id):
        log.info("eval_skipped_feature_flag", product=product.id)
        return {"counters": {"evaluated": 0, "errors": 0}, "summary": None}

    cutoff = resolve_cutoff_date(product.product_meta)
    all_snippets = list(product.snippets)
    golden = golden_set_subset(all_snippets, cutoff)

    log.info(
        "eval_start",
        product=product.id, run_id=run_id,
        n_snippets_total=len(all_snippets), n_golden=len(golden),
        cutoff=cutoff.isoformat() if cutoff else None,
    )

    if not all_snippets:
        summary = EvalSummary(
            status="no_snippets",
            product_id=product.id,
            run_id=run_id,
            n_snippets_total=0,
            n_golden=0,
            n_evaluated=0,
            n_errors=0,
            cutoff_date=cutoff.isoformat() if cutoff else None,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        _write_summary(summary)
        return {"counters": {"evaluated": 0, "errors": 0}, "summary": summary.to_dict()}

    per_item = _classify_golden_set(golden)
    summary = compute_summary(
        per_item,
        product_id=product.id,
        run_id=run_id,
        n_snippets_total=len(all_snippets),
        n_golden=len(golden),
        cutoff_date=cutoff,
    )

    if summary.status == "ok":
        history = _load_recent_history(product.id, exclude_run_id=run_id, limit=3)
        summary.regressions = detect_regressions(summary.metrics, history)

    _write_summary(summary)
    log.info(
        "eval_done",
        product=product.id, run_id=run_id, status=summary.status,
        evaluated=summary.n_evaluated, errors=summary.n_errors,
        regressions=len(summary.regressions),
    )
    return {
        "counters": {"evaluated": summary.n_evaluated, "errors": summary.n_errors},
        "summary": summary.to_dict(),
    }


def _classify_golden_set(golden: list[Any]) -> list[EvalPerItem]:
    """Run classify against each golden snippet. Isolates per-item errors
    so one bad snippet doesn't nuke the whole eval."""
    from pipeline.classify import classify_one
    from pipeline.llm import LLMClient

    client = LLMClient("classify")
    per_item: list[EvalPerItem] = []
    for snippet in golden:
        item = snippet.to_classify_item()
        try:
            predicted, regex_res, primary = classify_one(item, client)
        except Exception as e:
            per_item.append(EvalPerItem(
                id=snippet.id,
                areas=(0, 0, 0),
                content_types=(0, 0, 0),
                entities=(0, 0, 0),
                kb=(0, 0, 0),
                primary_area_correct=None,
                sentiment_3class_correct=None,
                sentiment_sign_correct=None,
                severity_correct=None,
                error=str(e),
            ))
            continue
        per_item.append(score_prediction(
            snippet_id=snippet.id,
            predicted=predicted,
            regex_res=regex_res,
            primary_area=primary,
            gold_labels=snippet.labels or {},
        ))
    return per_item


def _write_summary(summary: EvalSummary) -> None:
    """Persist the machine-readable summary. Best-effort; failure is logged
    but doesn't crash the pipeline."""
    try:
        path = _summary_path(summary.product_id, summary.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(summary.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception as e:
        log.warning("eval_summary_write_failed", error=str(e))


def load_summary(product_id: str, run_id: str) -> Optional[dict[str, Any]]:
    """Read the machine-readable summary for a completed run. Used by the
    run detail template's scorecard card."""
    path = _summary_path(product_id, run_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_recent_history(
    product_id: str, exclude_run_id: str, limit: int = 3,
) -> list[dict[str, dict[str, Any]]]:
    """Return metric dicts from the last N completed runs (excluding this one).

    Ordered oldest-to-newest so `history[-3:]` gives the most recent trio.
    """
    from pipeline.config import app_config, resolve_path
    data_root = resolve_path(app_config()["paths"]["data_root"])
    temp_runs = data_root / product_id / "temp_runs"
    if not temp_runs.exists():
        return []
    candidates = []
    for run_dir in temp_runs.iterdir():
        if not run_dir.is_dir() or run_dir.name == exclude_run_id:
            continue
        summary_path = run_dir / "eval_summary.json"
        if not summary_path.exists():
            continue
        try:
            data = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("status") != "ok":
            continue
        candidates.append((data.get("generated_at") or "", data.get("metrics") or {}))
    candidates.sort()               # oldest first
    return [m for _, m in candidates[-limit:]]


# ---------------------------------------------------------------------------
# §4.5 API — dry run without persisting
# ---------------------------------------------------------------------------


def evals_dry_run(
    prompt_version: str,
    model: str,
    *,
    override_prompts: Optional[dict[str, Any]] = None,
) -> EvalSummary:
    """Score the current golden set with a hypothetical prompt/model
    configuration. Used by §4.5 prompt suggestions to answer
    "if we accept this diff, does our eval improve or regress?"

    Doesn't write to disk; doesn't consult the feature flag; doesn't touch
    run history. Caller is responsible for restoring anything they mutate
    (prompt file, model routing) after the dry run completes.

    `prompt_version` and `model` are recorded in the returned summary for
    later diff comparison; they don't affect the classify call directly
    (the caller sets those up separately).
    """
    _ = prompt_version, model, override_prompts   # accepted for API stability
    from pipeline.config import current_product
    from pipeline.snippets import golden_set_subset

    product = current_product()
    cutoff = resolve_cutoff_date(product.product_meta)
    all_snippets = list(product.snippets)
    golden = golden_set_subset(all_snippets, cutoff)

    per_item = _classify_golden_set(golden) if golden else []
    return compute_summary(
        per_item,
        product_id=product.id,
        run_id=f"dry:{prompt_version}",
        n_snippets_total=len(all_snippets),
        n_golden=len(golden),
        cutoff_date=cutoff,
    )
