from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from jsonschema import Draft202012Validator, ValidationError

from nyanpasu_github_reviewer.prompt import TEMPLATES_DIR

if TYPE_CHECKING:
    from jsonschema.protocols import Validator


def _read(name: str) -> dict:
    return json.loads((TEMPLATES_DIR / name).read_text())


@pytest.fixture
def validator() -> Validator:
    schema = _read("review.schema.json")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_ci_is_optional_and_can_concern_a_newer_head_than_review(validator: Validator) -> None:
    report = _read("review.example.json")
    ci = report.pop("ci")
    validator.validate(report)

    # CI may concern a newer head than the last completed review.
    ci["source"]["head_sha"] = "a" * 40
    report["ci"] = ci
    validator.validate(report)
    del report["ci"]
    validator.validate(report)


@pytest.mark.parametrize("status", ["analyzing", "completed", "incomplete"])
def test_current_failure_remains_visible_through_analysis_states(validator: Validator, status: str) -> None:
    report = _read("review.example.json")
    report["ci"]["status"] = status
    if status != "completed":
        failure = next(iter(report["ci"]["failures"].values()))
        failure.pop("evidence")
        failure.pop("next_step")
    validator.validate(report)


def test_empty_or_successful_ci_is_not_a_visible_section(validator: Validator) -> None:
    report = _read("review.example.json")
    report["ci"]["failures"] = {}
    with pytest.raises(ValidationError):
        validator.validate(report)

    report = _read("review.example.json")
    next(iter(report["ci"]["failures"].values()))["conclusion"] = "success"
    with pytest.raises(ValidationError):
        validator.validate(report)


@pytest.mark.parametrize("field", ["evidence", "next_step", "tested_sha"])
def test_completed_ci_requires_evidence_and_execution_identity(validator: Validator, field: str) -> None:
    report = _read("review.example.json")
    next(iter(report["ci"]["failures"].values())).pop(field)
    with pytest.raises(ValidationError):
        validator.validate(report)


def test_ci_only_dashboard_does_not_invent_review_coverage(validator: Validator) -> None:
    report = _read("review.example.json")
    report.update(status="not_reviewed", summary="尚未进行代码审查。", findings={})
    report.pop("simplification")
    report["source"].pop("merge_base_sha")
    report["source"].pop("inventory_id")
    for stage in report["stages"].values():
        stage.update(status="pending", summary="尚未进行代码审查。")
    validator.validate(report)
    report.pop("ci")
    validator.validate(report)

    report["stages"]["general"]["status"] = "completed"
    with pytest.raises(ValidationError):
        validator.validate(report)


@pytest.mark.parametrize(
    "status", ["reviewing", "preliminary", "approved", "changes_requested", "comment", "incomplete"]
)
def test_review_outcomes_still_require_pinned_source(validator: Validator, status: str) -> None:
    report = _read("review.example.json")
    report["status"] = status
    report["source"].pop("merge_base_sha")
    report["source"].pop("inventory_id")
    errors = list(validator.iter_errors(report))
    assert any(list(error.path) == ["source"] and error.validator == "required" for error in errors)
