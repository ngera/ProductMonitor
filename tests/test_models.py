"""Tests for §4.6.1 conditional-field normalization (pure, model-independent)."""

from pipeline.models import Classification, Entity, normalize_classification


def _base(**kw) -> Classification:
    data = {"is_windows_relevant": True, "areas": ["audio"], "content_types": ["feedback"]}
    data.update(kw)
    return Classification(**data)


def test_bug_fields_cleared_when_not_a_bug():
    c = _base(content_types=["feedback"], bug_severity="high", bug_is_regression=True)
    norm, report = normalize_classification(c)
    assert norm.bug_severity is None
    assert norm.bug_is_regression is None
    assert report.conditional_violations == 1


def test_bug_without_severity_defaults_low():
    c = _base(content_types=["bug_report"], bug_severity=None)
    norm, report = normalize_classification(c)
    assert norm.bug_severity == "low"
    assert norm.bug_repro_steps_quality == "none"
    assert report.low_confidence_bug is True


def test_bug_fields_preserved_when_bug():
    c = _base(content_types=["bug_report"], bug_severity="critical")
    norm, _ = normalize_classification(c)
    assert norm.bug_severity == "critical"


def test_request_fields_cleared_when_not_request():
    c = _base(content_types=["feedback"], request_specificity="concrete")
    norm, report = normalize_classification(c)
    assert norm.request_specificity is None
    assert report.conditional_violations == 1


def test_low_confidence_feature_implicated_demoted():
    ent = Entity(type="driver", vendor="Intel", product="AX211", role="feature_implicated",
                 confidence=0.3, verbatim="Intel AX211")
    c = _base(content_types=["bug_report"], bug_severity="high", entities=[ent])
    norm, report = normalize_classification(c, feature_implicated_min_confidence=0.5)
    assert norm.entities[0].role in ("hardware_in_use", "software_in_use")
    assert report.demoted_entities == 1


def test_high_confidence_feature_implicated_kept():
    ent = Entity(type="driver", vendor="Intel", product="AX211", role="feature_implicated",
                 confidence=0.9, verbatim="Intel AX211")
    c = _base(content_types=["bug_report"], bug_severity="high", entities=[ent])
    norm, _ = normalize_classification(c)
    assert norm.entities[0].role == "feature_implicated"
