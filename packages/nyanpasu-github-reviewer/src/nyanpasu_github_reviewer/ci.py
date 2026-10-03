from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Literal

from nyanpasu_github.gh import gh_json
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from nyanpasu_github.models import PullRequestRef

    from nyanpasu_github_reviewer.models import GitHubReviewerConfig


class CIHeadChanged(ValueError):
    """The PR no longer has the source requested by the caller."""


class CIFailure(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    kind: Literal["check", "status"]
    name: str
    conclusion: str
    url: str
    tested_sha: str
    run_id: int | None = None
    run_attempt: int | None = None
    completed_at: str | None = None


class CISnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    head_sha: str
    failures: tuple[CIFailure, ...] = ()

    @property
    def fingerprint(self) -> str:
        payload = {
            "head_sha": self.head_sha,
            "failures": sorted((item.model_dump() for item in self.failures), key=lambda item: item["id"]),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_PR_QUERY = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      headRefOid
      commits(last:1) { nodes { commit { id oid } } }
      potentialMergeCommit { id oid parents(first:2) { nodes { oid } } }
    }
  }
}
"""

_CHECKS_QUERY = """
query($id:ID!, $cursor:String) {
  node(id:$id) {
    ... on Commit {
      oid
      statusCheckRollup {
        contexts(first:100, after:$cursor) {
          nodes {
            __typename
            ... on CheckRun {
              databaseId name status conclusion detailsUrl completedAt
              checkSuite {
                app { databaseId }
                repository { nameWithOwner }
                workflowRun { databaseId workflow { databaseId } }
              }
            }
            ... on StatusContext { id context state targetUrl createdAt }
          }
          pageInfo { hasNextPage endCursor }
        }
      }
    }
  }
}
"""

_FAILURES = {"FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}


def _version(value: str | None) -> str:
    return hashlib.sha256((value or "").encode()).hexdigest()[:12]


def _graphql(config: GitHubReviewerConfig, query: str, **variables: str | int) -> dict[str, Any]:
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        args.extend(["-F" if isinstance(value, int) else "-f", f"{key}={value}"])
    result = gh_json(args, env=config.gh_env)
    if not isinstance(result, dict) or result.get("errors") or not isinstance(result.get("data"), dict):
        raise ValueError("GitHub did not return a complete CI query result")
    return result["data"]


def _pr_commits(config: GitHubReviewerConfig, pr: PullRequestRef) -> tuple[dict[str, Any], ...]:
    owner, name = pr.repo.split("/", 1)
    data = _graphql(config, _PR_QUERY, owner=owner, name=name, number=pr.number)
    repository = data.get("repository")
    current = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(current, dict):
        raise ValueError("GitHub did not return the requested PR")
    if current["headRefOid"] != pr.head_sha:
        raise CIHeadChanged(f"CI source changed for {pr.repo}#{pr.number}")
    head = current["commits"]["nodes"][-1]["commit"]
    if head["oid"] != pr.head_sha:
        raise CIHeadChanged(f"CI commit changed for {pr.repo}#{pr.number}")
    commits = [head]
    merge = current["potentialMergeCommit"]
    # A merge test is relevant only while it still contains this exact PR head.
    if merge is not None and pr.head_sha in {item["oid"] for item in merge["parents"]["nodes"]}:
        commits.append({"id": merge["id"], "oid": merge["oid"]})
    return tuple(commits)


def _commit_checks(config: GitHubReviewerConfig, commit: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    variables = {"id": commit["id"]}
    while True:
        data = _graphql(config, _CHECKS_QUERY, **variables)
        node = data.get("node")
        if not isinstance(node, dict) or node.get("oid") != commit["oid"]:
            raise ValueError("GitHub did not return the requested CI commit")
        rollup = node["statusCheckRollup"]
        if rollup is None:
            return nodes
        contexts = rollup["contexts"]
        nodes.extend(contexts["nodes"])
        page = contexts["pageInfo"]
        if not page["hasNextPage"]:
            return nodes
        variables["cursor"] = page["endCursor"]


def _latest_checks(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select replacements before filtering failures, including queued replacements."""
    latest: dict[tuple, dict[str, Any]] = {}
    newest_runs: dict[tuple, int] = {}
    for item in nodes:
        if item["__typename"] == "CheckRun":
            suite = item["checkSuite"]
            run = suite["workflowRun"]
            workflow = run["workflow"]["databaseId"] if run else None
            group = (suite["repository"]["nameWithOwner"], suite["app"]["databaseId"], workflow)
            key = ("check", *group, item["name"])
            version = (item["source_rank"], int(item["databaseId"]))
            if run:
                run_key = (*group, item["source_rank"])
                newest_runs[run_key] = max(newest_runs.get(run_key, 0), int(run["databaseId"]))
        elif item["__typename"] == "StatusContext":
            key = ("status", item["context"])
            version = (item["source_rank"], item["createdAt"], item["id"])
        else:
            raise ValueError("GitHub returned an unknown CI context")
        if key not in latest or version > latest[key]["version"]:
            latest[key] = {**item, "version": version}
    selected = []
    for item in latest.values():
        if item["__typename"] == "CheckRun":
            suite = item["checkSuite"]
            run = suite["workflowRun"]
            if run:
                run_key = (
                    suite["repository"]["nameWithOwner"],
                    suite["app"]["databaseId"],
                    run["workflow"]["databaseId"],
                    item["source_rank"],
                )
                if run["databaseId"] != newest_runs[run_key]:
                    continue
        selected.append(item)
    return selected


def _run_attempt(config: GitHubReviewerConfig, repo: str, run_id: int) -> tuple[int, dict[str, dict[str, Any]]]:
    run = gh_json(["api", f"repos/{repo}/actions/runs/{run_id}"], env=config.gh_env)
    attempt = int(run["run_attempt"])
    if attempt == 1:
        return attempt, {}
    # A rerun can replace only one job. Read every execution and retain each job's
    # latest attempt instead of dropping failures omitted from a partial rerun.
    latest: dict[str, dict[str, Any]] = {}
    page = 1
    while True:
        result = gh_json(
            ["api", f"repos/{repo}/actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}"],
            env=config.gh_env,
        )
        jobs = result["jobs"]
        for job in jobs:
            previous = latest.get(job["name"])
            if previous is None or (job["run_attempt"], job["id"]) > (previous["run_attempt"], previous["id"]):
                latest[job["name"]] = job
        if len(jobs) < 100:
            return attempt, latest
        page += 1


def fetch_ci_snapshot(config: GitHubReviewerConfig, pr: PullRequestRef) -> CISnapshot:
    """Read checks associated with the PR's current head and current merge test.

    A failed read raises; callers must preserve their previous observation. No
    branch-name or workflow-head-SHA inference is used to associate fork CI.
    """
    commits = _pr_commits(config, pr)
    checks = [
        {**item, "tested_sha": commit["oid"], "source_rank": rank}
        for rank, commit in enumerate(commits)
        for item in _commit_checks(config, commit)
    ]
    failures: list[CIFailure] = []
    attempts: dict[tuple[str, int], tuple[int, dict[str, dict[str, Any]]]] = {}
    for item in _latest_checks(checks):
        if item["__typename"] == "StatusContext":
            if item["state"] in _FAILURES:
                failures.append(
                    CIFailure(
                        id=f"status:{item['id']}:{_version(item['createdAt'])}",
                        kind="status",
                        name=item["context"],
                        conclusion=item["state"].lower(),
                        url=item["targetUrl"] or f"{pr.url}/checks",
                        tested_sha=item["tested_sha"],
                    )
                )
            continue
        if item["status"] != "COMPLETED" or item["conclusion"] not in _FAILURES:
            continue
        suite = item["checkSuite"]
        run = suite["workflowRun"]
        run_id = int(run["databaseId"]) if run else None
        attempt = None
        if run_id is not None:
            key = (suite["repository"]["nameWithOwner"], run_id)
            if key not in attempts:
                attempts[key] = _run_attempt(config, *key)
            run_attempt, jobs = attempts[key]
            job = jobs.get(item["name"])
            if job is not None:
                if job["status"] != "completed" or str(job["conclusion"]).upper() not in _FAILURES:
                    continue
                attempt = int(job["run_attempt"])
                replacement: dict[str, Any] = {
                    **item,
                    "databaseId": int(job["check_run_url"].rsplit("/", 1)[-1]),
                    "conclusion": str(job["conclusion"]).upper(),
                    "detailsUrl": job["html_url"],
                    "completedAt": job["completed_at"],
                }
                item = replacement
            else:
                # Missing jobs are not evidence of recovery or replacement.
                attempt = 1 if run_attempt == 1 else None
        failures.append(
            CIFailure(
                id=f"check:{item['databaseId']}:"
                + (f"attempt:{attempt}" if attempt is not None else _version(item["completedAt"])),
                kind="check",
                name=item["name"],
                conclusion=item["conclusion"].lower(),
                url=item["detailsUrl"] or f"{pr.url}/checks",
                tested_sha=item["tested_sha"],
                run_id=run_id,
                run_attempt=attempt,
                completed_at=item["completedAt"],
            )
        )
    if _pr_commits(config, pr) != commits:
        raise CIHeadChanged(f"CI comparison changed for {pr.repo}#{pr.number}")
    failures.sort(key=lambda failure: failure.id)
    return CISnapshot(head_sha=pr.head_sha, failures=tuple(failures))
