from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import TYPE_CHECKING, Any

from nyanpasu_github.gh import GitHubCommandError

if TYPE_CHECKING:
    from nyanpasu_github_reviewer.ci import CISnapshot
    from nyanpasu_github_reviewer.models import GitHubReviewerConfig, PullRequestRef


def reuse_ci_section(snapshot: CISnapshot, previous: dict[str, Any] | None) -> dict[str, Any] | None:
    if previous is None:
        return None
    failures = {}
    for failure in snapshot.failures:
        entry = previous["failures"].get(failure.id)
        if entry is None or any(
            entry.get(key) != getattr(failure, key) for key in ("tested_sha", "run_id", "run_attempt", "conclusion")
        ):
            return None
        failures[failure.id] = {**entry, "name": failure.name, "url": failure.url}
    return {
        "source": {"head_sha": snapshot.head_sha},
        "snapshot_fingerprint": snapshot.fingerprint,
        "status": previous["status"],
        "failures": failures,
    }


def read_review_dashboard(config: GitHubReviewerConfig, pr: PullRequestRef) -> dict[str, Any] | None:
    result = _slate(config, pr, "view")
    if result.returncode:
        try:
            error = json.loads(result.stderr)
        except json.JSONDecodeError:
            error = {}
        if error.get("error", {}).get("code") == "slate_not_found":
            return None
        raise GitHubCommandError(result.args, result.returncode, result.stdout, result.stderr)
    dashboard = json.loads(result.stdout)
    if dashboard["status"] != "valid":
        raise ValueError(f"Review dashboard for {pr.key} has visible drift")
    return dashboard


def apply_ci_patch(
    config: GitHubReviewerConfig,
    pr: PullRequestRef,
    revision: int,
    patch: list[dict[str, Any]],
    *,
    preview: bool = False,
) -> None:
    args = [
        "--patch",
        "-",
        "--if-revision",
        str(revision),
    ]
    if preview:
        args.append("--dry-run")
    result = _slate(config, pr, "apply", *args, input_text=json.dumps(patch))
    if result.returncode:
        raise GitHubCommandError(result.args, result.returncode, result.stdout, result.stderr)


def _slate(
    config: GitHubReviewerConfig, pr: PullRequestRef, command: str, *args: str, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    cli = ["gh-slate"] if shutil.which("gh-slate") else ["gh", "slate"]
    argv = [*cli, command, "nyanpasu-review", "--target", pr.url, "--json", *args]
    if config.github_login:
        argv.extend(["--controller", config.github_login])
    return subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=60,
        env=os.environ | config.gh_env if config.gh_env is not None else None,
        check=False,
    )
