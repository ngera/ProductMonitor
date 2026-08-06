"""Sample-snippet subsystem (Phase 5).

Snippets are user-authored labeled examples that serve two purposes:

  1. Few-shot injection into the Relevance and Classify prompts so the LLM
     sees concrete in-topic vs off-topic examples. Improves accuracy without
     fine-tuning and dampens drift when the model changes.
  2. Eval gold for the eval harness. Snippets marked `holdout_eval: true` are
     reserved from few-shot use and graded against by `eval/run_eval.py`.

Snippets live under `topics/<id>/examples/{positive,negative}/<slug>.yaml`.
YAML shape (per file):

    source_url: https://www.reddit.com/r/Windows11/comments/...   # optional
    title: "Bluetooth audio stuttering after KB5036980"            # optional
    body: |
      Free-form text content. Required when source_url is omitted (the user
      pasted a synthetic example rather than a real source URL).
    polarity: positive_example                                     # required
    holdout_eval: false                                            # default false
    labels:                                                        # required
      is_topic_relevant: true
      areas: [audio, network]
      content_types: [bug_report]
      sentiment: -0.6
      summary: "..."
      bug_severity: high
      entities:
        - type: bluetooth_adapter
          role: feature_implicated
          verbatim: "Bluetooth"
      extras:
        windows_major: win11
        windows_feature_update: "24H2"
    notes: "Example: hardware regression after KB."

`polarity` is one of:
  - `positive_example`: in-topic, classifier should reproduce labels.
  - `negative_example`: off-topic, relevance gate should drop it.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

POSITIVE = "positive_example"
NEGATIVE = "negative_example"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class Snippet:
    """One labeled example loaded from disk."""

    id: str                        # filename stem
    polarity: str                  # POSITIVE | NEGATIVE
    source_url: Optional[str]
    title: Optional[str]
    body: str
    labels: dict[str, Any]
    holdout_eval: bool = False
    notes: str = ""

    # Path-on-disk; useful for the UI's edit/delete flows.
    path: Optional[Path] = None

    # POST_V1_PLAN §4.10 D15 — time-based golden-set split. Populated from
    # the YAML's `created_at:` if present, else the file's mtime. Snippets
    # authored before the product's cutoff are golden; after = training.
    created_at: Optional[datetime] = None

    @property
    def is_positive(self) -> bool:
        return self.polarity == POSITIVE

    @property
    def is_negative(self) -> bool:
        return self.polarity == NEGATIVE

    def to_classify_item(self) -> dict[str, Any]:
        """Shape a Snippet so classify_one() can score it during eval."""
        return {
            "id": self.id,
            "title": self.title or "",
            "body": self.body,
            "source_display_name": "snippet",
            "source": "snippet",
            "raw": {},
            "labels": self.labels,
        }


def slugify(s: str) -> str:
    """Convert a free-form string into a filesystem-safe slug for snippet ids."""
    return _SLUG_RE.sub("-", s.lower()).strip("-") or "snippet"


def load_snippets(topic_dir: Path) -> list[Snippet]:
    """Read every snippet YAML under topics/<id>/examples/{positive,negative}/."""
    examples_root = topic_dir / "examples"
    if not examples_root.exists():
        return []
    snippets: list[Snippet] = []
    for polarity_dir, polarity in (
        (examples_root / "positive", POSITIVE),
        (examples_root / "negative", NEGATIVE),
    ):
        if not polarity_dir.exists():
            continue
        for path in sorted(polarity_dir.glob("*.yaml")):
            try:
                snippets.append(_parse_one(path, polarity))
            except Exception as e:
                # A bad single file shouldn't break loading; the UI surfaces it.
                snippets.append(
                    Snippet(
                        id=path.stem,
                        polarity=polarity,
                        source_url=None,
                        title=None,
                        body=f"[PARSE ERROR] {e}",
                        labels={},
                        holdout_eval=False,
                        notes=str(e),
                        path=path,
                    )
                )
    return snippets


def _parse_one(path: Path, polarity: str) -> Snippet:
    blob: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    body = blob.get("body") or ""
    if not body and not blob.get("source_url"):
        raise ValueError(f"snippet {path} has neither body nor source_url")
    created_at = _parse_created_at(blob.get("created_at"), path)
    return Snippet(
        id=path.stem,
        polarity=polarity,
        source_url=blob.get("source_url"),
        title=blob.get("title"),
        body=body,
        labels=blob.get("labels") or {},
        holdout_eval=bool(blob.get("holdout_eval", False)),
        notes=str(blob.get("notes") or ""),
        path=path,
        created_at=created_at,
    )


def _parse_created_at(raw: Any, path: Path) -> datetime:
    """Snippet YAMLs may declare an explicit `created_at:`; else we use the
    file's mtime (§4.10 migration). Always tz-aware UTC."""
    if raw is not None:
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        if isinstance(raw, str):
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
    try:
        mtime = path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


def save_snippet(topic_dir: Path, snippet: Snippet) -> Path:
    """Write a snippet to disk under examples/<polarity>/<id>.yaml.

    Used by the admin UI's snippet form. Existing files are overwritten
    (idempotent edit-by-id).
    """
    if snippet.polarity not in (POSITIVE, NEGATIVE):
        raise ValueError(f"unknown polarity: {snippet.polarity!r}")
    target_dir = topic_dir / "examples" / ("positive" if snippet.is_positive else "negative")
    target_dir.mkdir(parents=True, exist_ok=True)
    blob = {
        "source_url": snippet.source_url,
        "title": snippet.title,
        "body": snippet.body,
        "polarity": snippet.polarity,
        "holdout_eval": snippet.holdout_eval,
        "labels": snippet.labels,
        "notes": snippet.notes or "",
    }
    out = target_dir / f"{snippet.id}.yaml"
    out.write_text(
        yaml.safe_dump(blob, sort_keys=False, allow_unicode=True, default_flow_style=False),
        encoding="utf-8",
    )
    return out


def delete_snippet(snippet: Snippet) -> None:
    if snippet.path and snippet.path.exists():
        snippet.path.unlink()


def few_shot_subset(
    snippets: list[Snippet],
    n_positive: int,
    n_negative: int,
    *,
    exclude_holdout: bool = True,
    seed: int = 42,
) -> list[Snippet]:
    """Deterministically pick a few-shot subset.

    Held-out snippets are excluded by default so eval gold doesn't leak into
    training-time context. The deterministic seed keeps prompts stable
    run-over-run (drift detection benefits from this).
    """
    rng = random.Random(seed)
    pool_pos = [s for s in snippets if s.is_positive and (not exclude_holdout or not s.holdout_eval)]
    pool_neg = [s for s in snippets if s.is_negative and (not exclude_holdout or not s.holdout_eval)]
    pos_subset = rng.sample(pool_pos, k=min(n_positive, len(pool_pos)))
    neg_subset = rng.sample(pool_neg, k=min(n_negative, len(pool_neg)))
    return pos_subset + neg_subset


def holdout_subset(snippets: list[Snippet]) -> list[Snippet]:
    """Snippets reserved for eval. Used by the eval harness."""
    return [s for s in snippets if s.holdout_eval]


# ---------------------------------------------------------------------------
# Time-based golden-set split (POST_V1_PLAN §4.10 D15)
# ---------------------------------------------------------------------------


def golden_set_subset(
    snippets: list[Snippet],
    cutoff_date: Optional[datetime],
) -> list[Snippet]:
    """Snippets authored on or before `cutoff_date` — the eval "golden set".

    Time-based (not user-picked) so training and holdout don't share the
    author's selection bias. When cutoff_date is None, every snippet is
    considered golden — useful in tests and for the first months of a
    product's life before there are enough post-cutoff examples to
    distinguish.
    """
    if cutoff_date is None:
        return list(snippets)
    if cutoff_date.tzinfo is None:
        cutoff_date = cutoff_date.replace(tzinfo=timezone.utc)
    return [s for s in snippets if s.created_at is not None and s.created_at <= cutoff_date]


def training_subset(
    snippets: list[Snippet],
    cutoff_date: Optional[datetime],
) -> list[Snippet]:
    """Snippets authored after `cutoff_date` — the few-shot training pool."""
    if cutoff_date is None:
        return []
    if cutoff_date.tzinfo is None:
        cutoff_date = cutoff_date.replace(tzinfo=timezone.utc)
    return [s for s in snippets if s.created_at is not None and s.created_at > cutoff_date]


# --- Few-shot block rendering ------------------------------------------------


def render_relevance_few_shot(picked: list[Snippet]) -> str:
    """Plain-text examples for the relevance prompt.

    Each line: TITLE / BODY excerpt / relevant: true|false.
    """
    if not picked:
        return ""
    lines = ["EXAMPLES (for calibration):"]
    for i, s in enumerate(picked, 1):
        relevant = s.labels.get("is_topic_relevant") if s.labels else (True if s.is_positive else False)
        excerpt = (s.body or "")[:200].replace("\n", " ").strip()
        lines.append(
            f"  [{i}] title: {s.title or '(no title)'}\n"
            f"      body: {excerpt}\n"
            f"      -> relevant: {str(bool(relevant)).lower()}"
        )
    lines.append("")
    return "\n".join(lines)


def render_classify_few_shot(picked: list[Snippet]) -> str:
    """JSON-style examples for the classify prompt.

    Shows each example's body excerpt and the full labels dict as JSON so
    the LLM sees the schema shape applied to real data. We deliberately
    include negative examples too — they teach the model what NOT to fill in.
    """
    if not picked:
        return ""
    lines = ["EXAMPLES (label these the way the user would):"]
    for i, s in enumerate(picked, 1):
        excerpt = (s.body or "")[:300].replace("\n", " ").strip()
        labels_json = json.dumps(s.labels, indent=2, ensure_ascii=False) if s.labels else "{}"
        lines.append(
            f"  [{i}] title: {s.title or '(no title)'}\n"
            f"      body: {excerpt}\n"
            f"      labels: {labels_json}"
        )
    lines.append("")
    return "\n".join(lines)
