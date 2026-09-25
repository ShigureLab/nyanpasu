from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator, ValidationError

from nyanpasu_github_reviewer.prompt import TEMPLATES_DIR


@pytest.fixture
def dashboard():
    return json.loads((TEMPLATES_DIR / "review.example.json").read_text())


@pytest.fixture(scope="module")
def validator():
    schema = json.loads((TEMPLATES_DIR / "review.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_general_can_publish_without_waiting_for_necessity_audit(dashboard, validator):
    dashboard["status"] = "preliminary"
    dashboard["stages"]["deep"]["status"] = "running"
    del dashboard["simplification"]
    validator.validate(dashboard)

    dashboard["status"] = "comment"
    dashboard["stages"]["deep"]["status"] = "completed"
    with pytest.raises(ValidationError):
        validator.validate(dashboard)


@pytest.mark.parametrize("scope", ["production", "tests"])
@pytest.mark.parametrize("gap", ["missing", "pending", "running", "incomplete", "empty", "unevidenced"])
def test_deep_completion_rejects_unfinished_or_unrecorded_audit(dashboard, validator, scope, gap):
    audit = dashboard["simplification"][scope]
    if gap == "missing":
        del dashboard["simplification"][scope]
    elif gap == "empty":
        audit["entries"] = []
    elif gap == "unevidenced":
        del audit["entries"][0]["evidence"]
    else:
        audit["status"] = gap
    with pytest.raises(ValidationError):
        validator.validate(dashboard)


def test_retention_can_complete_review_without_any_findings(dashboard, validator):
    dashboard["status"] = "approved"
    dashboard["findings"] = {}
    for audit in dashboard["simplification"].values():
        audit["summary"] = "Retain after tracing callers and comparing the smaller alternative."
        for entry in audit["entries"]:
            entry["decision"] = "retain"
            entry["evidence"] = "The shared helper cannot cover the entrypoint conversion contract."
    validator.validate(dashboard)


def test_no_applicable_scope_needs_a_reason_and_no_invented_decisions(dashboard, validator):
    dashboard["simplification"]["tests"] = {
        "status": "skipped",
        "summary": "Documentation-only scope with no related executable tests.",
        "entries": [],
    }
    validator.validate(dashboard)
    dashboard["simplification"]["tests"]["summary"] = ""
    with pytest.raises(ValidationError):
        validator.validate(dashboard)


def test_partial_audit_preserves_delivered_general_result(dashboard, validator):
    dashboard["status"] = "incomplete"
    dashboard["stages"]["deep"]["status"] = "incomplete"
    dashboard["simplification"]["tests"]["status"] = "incomplete"
    dashboard["simplification"]["tests"]["summary"] = "Fixture consolidation still needs an unavailable runtime."
    validator.validate(dashboard)


def test_simplification_is_nonblocking_and_old_findings_need_no_kind(dashboard, validator):
    validator.validate(dashboard)
    dashboard["findings"]["F1"]["priority"] = "P1"
    with pytest.raises(ValidationError):
        validator.validate(dashboard)
    del dashboard["findings"]["F1"]["kind"]
    validator.validate(dashboard)
