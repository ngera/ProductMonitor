"""Pydantic + dataclass schemas for the pipeline.

Mirrors DESIGN.md §4.6 (Classification), §4.7 (Entity), §5.1 (RawItem).
Also implements the §4.6.1 conditional-field normalization that guided decoding
cannot enforce (business rules, not types).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

# --- Controlled vocabularies -------------------------------------------------

CONTENT_TYPES = {
    "bug_report",
    "feature_request",
    "feedback",
    "praise",
    "question",
    "workaround",
    "comparison",
    "news_discussion",
    "rant",
}

ENTITY_ROLES = {"feature_implicated", "hardware_in_use", "software_in_use"}

SEVERITY_VALUES = {"critical", "high", "medium", "low"}


# --- Raw ingestion -----------------------------------------------------------


@dataclass
class RawItem:
    """In-memory + one JSONL line. DESIGN.md §5.1.

    `url` is REQUIRED — the foundation of source attribution (§13).
    For comments, `raw["parent_context"]` carries the parent post's title +
    truncated body so the classifier can interpret bare replies (§4.3, §4.6).
    """

    source: str
    source_display_name: str
    external_id: str
    url: str
    parent_external_id: Optional[str]
    author: Optional[str]
    created_at: datetime
    title: Optional[str]
    body: str
    engagement: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError(
                f"RawItem.url is required (source={self.source}, "
                f"external_id={self.external_id})"
            )

    @property
    def item_id(self) -> str:
        return f"{self.source}:{self.external_id}"


# --- LLM relevance gate (§4.5) ----------------------------------------------


class RelevanceResult(BaseModel):
    relevant: bool
    confidence: float = Field(ge=0.0, le=1.0)


# --- LLM classify + extract (§4.6, §4.7) ------------------------------------


class Entity(BaseModel):
    type: str
    vendor: str
    product: Optional[str] = None
    version: Optional[str] = None
    role: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    verbatim: str

    @field_validator("role")
    @classmethod
    def _role_known(cls, v: str) -> str:
        if v not in ENTITY_ROLES:
            raise ValueError(f"unknown role: {v}")
        return v


class Classification(BaseModel):
    # Core
    is_windows_relevant: bool
    areas: list[str] = Field(default_factory=list)
    content_types: list[str] = Field(default_factory=list)
    sentiment: float = Field(ge=-1.0, le=1.0, default=0.0)
    summary: str = ""
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)

    # Always-attempted context
    user_context: str = "unknown"
    windows_major: str = "unknown"
    windows_feature_update: Optional[str] = None
    windows_build: Optional[str] = None
    windows_channel: Optional[str] = None
    windows_version_confidence: str = "unknown"

    # Bug-specific (only meaningful if "bug_report" in content_types)
    bug_severity: Optional[str] = None
    bug_is_regression: Optional[bool] = None
    bug_reproducibility: Optional[str] = None
    bug_repro_steps_quality: Optional[str] = None
    bug_repro_steps: Optional[list[str]] = None
    bug_preconditions: Optional[list[str]] = None

    # Request-specific (only meaningful if "feature_request" in content_types)
    request_specificity: Optional[str] = None
    request_existing_workaround: Optional[bool] = None

    # Entities
    entities: list[Entity] = Field(default_factory=list)


# --- §4.6.1 conditional-field normalization ----------------------------------


@dataclass
class NormalizationReport:
    """Counters surfaced to runs.completeness.conditional_violations."""

    conditional_violations: int = 0
    low_confidence_bug: bool = False
    demoted_entities: int = 0


def normalize_classification(
    c: Classification,
    *,
    feature_implicated_min_confidence: float = 0.5,
) -> tuple[Classification, NormalizationReport]:
    """Enforce conditional business rules guided decoding can't (DESIGN.md §4.6.1).

    Pure & deterministic — covered by tests/test_models.py, model-independent.
    Returns a (possibly mutated) copy and a report of what was corrected.
    """
    report = NormalizationReport()
    data = c.model_copy(deep=True)

    is_bug = "bug_report" in data.content_types
    is_request = "feature_request" in data.content_types

    # Rule 1: bug_* only valid for bug reports.
    bug_fields = [
        "bug_severity",
        "bug_is_regression",
        "bug_reproducibility",
        "bug_repro_steps_quality",
        "bug_repro_steps",
        "bug_preconditions",
    ]
    if not is_bug:
        if any(getattr(data, f) is not None for f in bug_fields):
            report.conditional_violations += 1
        for f in bug_fields:
            setattr(data, f, None)
    else:
        # Rule 2: a bug missing severity defaults to low + flag.
        if data.bug_severity is None:
            data.bug_severity = "low"
            data.bug_repro_steps_quality = data.bug_repro_steps_quality or "none"
            report.low_confidence_bug = True

    # Rule 3: request_* only valid for feature requests.
    request_fields = ["request_specificity", "request_existing_workaround"]
    if not is_request:
        if any(getattr(data, f) is not None for f in request_fields):
            report.conditional_violations += 1
        for f in request_fields:
            setattr(data, f, None)

    # Rule 4: demote low-confidence feature_implicated entities (§4.6.1)
    # so weak blame signals don't drive grouping (§4.8).
    for ent in data.entities:
        if (
            ent.role == "feature_implicated"
            and ent.confidence < feature_implicated_min_confidence
        ):
            # hardware-like types -> hardware_in_use, else software_in_use
            hardware_types = {
                "gpu", "cpu", "chipset", "motherboard", "laptop", "desktop",
                "tablet", "audio_device", "microphone", "headphone", "speaker",
                "display", "webcam", "printer", "peripheral", "dock_hub",
                "external_storage", "network_adapter", "bluetooth_adapter",
            }
            ent.role = "hardware_in_use" if ent.type in hardware_types else "software_in_use"
            report.demoted_entities += 1

    return data, report
