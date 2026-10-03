from __future__ import annotations

import copy

import pytest
from nyanpasu_github.models import PullRequestRef

from nyanpasu_github_reviewer.ci import CIHeadChanged, CISnapshot, fetch_ci_snapshot
from nyanpasu_github_reviewer.models import GitHubReviewerConfig


def _pr() -> PullRequestRef:
    return PullRequestRef(
        repo="base/repo",
        number=12,
        url="https://github.com/base/repo/pull/12",
        base_ref="main",
        head_ref="feature",
        head_sha="head",
        state="open",
        draft=False,
    )


def _check(id=10, *, name="test (linux)", status="COMPLETED", conclusion="FAILURE", run=50, workflow=5):
    return {
        "__typename": "CheckRun",
        "databaseId": id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "completedAt": "2026-10-03T00:00:00Z" if status == "COMPLETED" else None,
        "detailsUrl": f"https://github.com/base/repo/actions/runs/{run}/job/{id}",
        "checkSuite": {
            "app": {"databaseId": 1},
            "repository": {"nameWithOwner": "fork/repo"},
            "workflowRun": {"databaseId": run, "workflow": {"databaseId": workflow}} if run else None,
        },
    }


def _status(id="status-1", *, state="FAILURE", date="2026-10-03T00:00:00Z"):
    return {
        "__typename": "StatusContext",
        "id": id,
        "context": "external-build",
        "state": state,
        "targetUrl": None,
        "createdAt": date,
    }


def _job(id=10, *, name="test (linux)", attempt=1, status="completed", conclusion="failure"):
    return {
        "id": id,
        "name": name,
        "run_attempt": attempt,
        "status": status,
        "conclusion": conclusion,
        "check_run_url": f"https://api.github.com/repos/fork/repo/check-runs/{id}",
        "html_url": f"https://github.com/fork/repo/actions/runs/50/job/{id}",
        "completed_at": "2026-10-03T00:01:00Z" if status == "completed" else None,
    }


class GitHub:
    def __init__(self, checks=(), *, merge=None, merge_checks=(), attempt=1, current_jobs=()):
        self.checks = list(checks)
        self.merge = merge
        self.merge_checks = list(merge_checks)
        self.attempt = attempt
        self.current_jobs = current_jobs
        self.calls = []
        self.head = "head"
        self.pages = {}

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] != ["api", "graphql"]:
            path = args[1]
            if "/jobs?filter=all" in path:
                return {"jobs": self.current_jobs}
            assert path.startswith("repos/fork/repo/actions/runs/")
            # A failed job is useful while its sibling jobs still run.
            return {"run_attempt": self.attempt, "status": "in_progress"}
        fields = dict(part.split("=", 1) for part in args[3::2])
        if "pullRequest" in fields["query"]:
            return {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "headRefOid": self.head,
                            "commits": {"nodes": [{"commit": {"id": "head-node", "oid": self.head}}]},
                            "potentialMergeCommit": self.merge,
                        }
                    }
                }
            }
        oid = "head" if fields["id"] == "head-node" else "merge"
        if fields.get("cursor"):
            items = self.pages[fields["cursor"]]
            next_page = None
        else:
            items = self.checks if oid == "head" else self.merge_checks
            next_page = "page-2" if oid == "head" and self.pages else None
        return {
            "data": {
                "node": {
                    "oid": oid,
                    "statusCheckRollup": {
                        "contexts": {
                            "nodes": copy.deepcopy(items),
                            "pageInfo": {"hasNextPage": next_page is not None, "endCursor": next_page},
                        }
                    },
                }
            }
        }


def _fetch(monkeypatch, github):
    monkeypatch.setattr("nyanpasu_github_reviewer.ci.gh_json", github)
    return fetch_ci_snapshot(GitHubReviewerConfig(), _pr())


def test_snapshot_uses_current_merge_and_actual_check_repository(monkeypatch):
    github = GitHub(
        [_check(conclusion="SUCCESS")],
        merge={"id": "merge-node", "oid": "merge", "parents": {"nodes": [{"oid": "base"}, {"oid": "head"}]}},
        merge_checks=[_check(20)],
    )
    result = _fetch(monkeypatch, github)
    assert result.head_sha == "head"
    assert [(item.id, item.tested_sha, item.run_attempt) for item in result.failures] == [
        ("check:20:attempt:1", "merge", 1)
    ]
    assert ["api", "repos/fork/repo/actions/runs/50"] in github.calls


def test_unrelated_merge_commit_is_not_treated_as_current_ci(monkeypatch):
    result = _fetch(
        monkeypatch,
        GitHub(
            merge={"id": "merge-node", "oid": "merge", "parents": {"nodes": [{"oid": "base"}, {"oid": "old"}]}},
            merge_checks=[_check()],
        ),
    )
    assert not result.failures


def test_queued_replacements_suppress_old_failures_without_hiding_other_matrix_jobs(monkeypatch):
    result = _fetch(
        monkeypatch,
        GitHub(
            [
                _check(10),
                _check(11, status="QUEUED", conclusion=None),
                _check(12, name="test (windows)"),
                _status(),
                _status("status-2", state="PENDING", date="2026-10-03T00:01:00Z"),
            ]
        ),
    )
    assert [item.id for item in result.failures] == ["check:12:attempt:1"]


def test_latest_workflow_run_replaces_old_job_even_when_job_names_changed(monkeypatch):
    result = _fetch(
        monkeypatch,
        GitHub(
            [
                _check(10, name="old job", run=50),
                _check(11, name="new job", run=51, status="IN_PROGRESS", conclusion=None),
                _check(12, name="other workflow", workflow=6),
            ]
        ),
    )
    assert [item.id for item in result.failures] == ["check:12:attempt:1"]


def test_new_attempt_with_no_current_failure_does_not_republish_old_job(monkeypatch):
    result = _fetch(
        monkeypatch,
        GitHub(
            [_check(10)],
            attempt=2,
            current_jobs=[
                _job(),
                _job(11, attempt=2, status="queued", conclusion=None),
            ],
        ),
    )
    assert not result.failures


def test_failed_current_attempt_is_analyzable_before_workflow_completion(monkeypatch):
    result = _fetch(monkeypatch, GitHub([_check(10)], attempt=2, current_jobs=[_job(attempt=2)]))
    (failure,) = result.failures
    assert failure.run_id == 50 and failure.run_attempt == 2


def test_partial_rerun_preserves_unselected_failed_jobs_and_their_actual_attempt(monkeypatch):
    result = _fetch(
        monkeypatch,
        GitHub(
            [_check(10), _check(20, name="test (windows)")],
            attempt=2,
            current_jobs=[
                _job(10),
                _job(11, attempt=2, status="queued", conclusion=None),
                _job(20, name="test (windows)"),
            ],
        ),
    )
    (failure,) = result.failures
    assert failure.id == "check:20:attempt:1" and failure.run_attempt == 1


def test_missing_rerun_jobs_do_not_prove_recovery(monkeypatch):
    result = _fetch(monkeypatch, GitHub([_check(10)], attempt=2))
    assert len(result.failures) == 1 and result.failures[0].id.startswith("check:10:")


def test_reused_third_party_check_id_has_a_new_failure_fingerprint(monkeypatch):
    check = _check(run=None)
    original = _fetch(monkeypatch, GitHub([check]))
    check["completedAt"] = "2026-10-03T00:01:00Z"
    repeated = _fetch(monkeypatch, GitHub([check]))
    assert repeated.failures[0].id != original.failures[0].id
    assert repeated.fingerprint != original.fingerprint


def test_status_context_is_a_failure_without_an_actions_run(monkeypatch):
    result = _fetch(monkeypatch, GitHub([_status(state="ERROR")]))
    (failure,) = result.failures
    assert failure.id.startswith("status:status-1:") and failure.conclusion == "error"
    assert failure.run_id is None and failure.url == f"{_pr().url}/checks"


def test_rollup_paginates_before_deciding_that_ci_is_healthy(monkeypatch):
    github = GitHub([_check(conclusion="SUCCESS")])
    github.pages["page-2"] = [_check(20, name="last matrix job")]
    assert [item.id for item in _fetch(monkeypatch, github).failures] == ["check:20:attempt:1"]


@pytest.mark.parametrize("failure_state", ["CANCELLED", "SKIPPED", "NEUTRAL", "SUCCESS"])
def test_normal_cancellations_and_non_failures_do_not_trigger_analysis(monkeypatch, failure_state):
    assert not _fetch(monkeypatch, GitHub([_check(conclusion=failure_state)])).failures


def test_head_change_and_api_errors_raise_instead_of_reporting_recovery(monkeypatch):
    github = GitHub([_check()])
    github.head = "new-head"
    with pytest.raises(CIHeadChanged):
        _fetch(monkeypatch, github)
    with pytest.raises(ValueError, match="complete CI query"):
        _fetch(monkeypatch, lambda *_, **__: {"data": {}, "errors": [{"message": "rate limited"}]})


def test_failure_fingerprint_ignores_order_but_distinguishes_new_attempt(monkeypatch):
    result = _fetch(monkeypatch, GitHub([_check(10), _check(11, name="test (windows)")]))
    reordered = CISnapshot(head_sha=result.head_sha, failures=tuple(reversed(result.failures)))
    assert reordered.fingerprint == result.fingerprint
    rerun = result.model_copy(update={"failures": (result.failures[0].model_copy(update={"run_attempt": 2}),)})
    assert rerun.fingerprint != result.fingerprint
