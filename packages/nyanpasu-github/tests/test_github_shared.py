from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

import pytest
from nyanpasu_github.agent_tasks import (
    GitHubRepoConfigError,
    branch_agent_task,
    configured_branch_context,
    parse_pull_request_task_outcome,
)
from nyanpasu_github.gh import GitHubSignatureError, verify_webhook_signature
from nyanpasu_github.instructions import instruction_documents_for_repo
from nyanpasu_github.models import (
    GitHubRepoSettings,
    InstructionDocumentSettings,
    PullRequestRef,
    github_integration_from_config,
)
from nyanpasu_github.workspace import branch_workspace_ref, pull_request_workspace_ref

from nyanpasu.models import TaskAction

if TYPE_CHECKING:
    from pathlib import Path


def test_verify_webhook_signature() -> None:
    body = b'{"ok": true}'
    secret = "secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    verify_webhook_signature(body, f"sha256={digest}", secret)

    with pytest.raises(GitHubSignatureError):
        verify_webhook_signature(body, "sha256=bad", secret)


def test_instruction_documents_for_repo(tmp_path: Path) -> None:
    local = tmp_path / "repo"
    plugin_doc = tmp_path / "SOUL.md"
    repo_doc = tmp_path / "AGENTS.md"
    plugin_doc.write_text("plugin\n", encoding="utf-8")
    repo_doc.write_text("repo\n", encoding="utf-8")

    docs = instruction_documents_for_repo(
        repo="ExampleOrg/ExampleRepo",
        plugin_instruction_docs=(InstructionDocumentSettings(name="SOUL.md", path=plugin_doc),),
        repo_settings={
            "ExampleOrg/ExampleRepo": GitHubRepoSettings(
                local_path=local,
                instruction_docs=(InstructionDocumentSettings(name="AGENTS.md", path=repo_doc),),
            )
        },
    )

    assert [doc.name for doc in docs] == ["SOUL.md", "AGENTS.md"]
    assert [doc.content for doc in docs] == ["plugin\n", "repo\n"]


def test_workspace_refs(tmp_path: Path) -> None:
    settings = GitHubRepoSettings(local_path=tmp_path / "repo", github_remote="git@example.com:owner/repo.git")
    branch = branch_workspace_ref(repo="owner/repo", settings=settings, branch="develop", revision="abc")
    pr = PullRequestRef(
        repo="owner/repo",
        number=123,
        url="https://github.com/owner/repo/pull/123",
        base_ref="develop",
        head_ref="feature",
        head_sha="def",
        state="open",
        draft=False,
    )
    pr_workspace = pull_request_workspace_ref(pr, settings)

    assert branch.key == "owner/repo"
    assert branch.ref == "refs/heads/develop"
    assert branch.revision == "abc"
    assert pr_workspace.ref == "pull/123/head"
    assert pr_workspace.revision == "def"


def test_github_integration_config_resolves_auth_env(monkeypatch) -> None:
    monkeypatch.setenv("NYANPASU_TEST_GH_TOKEN", "env-token")

    config = github_integration_from_config(
        {
            "token_env": "NYANPASU_TEST_GH_TOKEN",
            "git_author_name": "Bot",
            "git_author_email": "bot@example.com",
        }
    )

    assert config.resolved_token == "env-token"
    assert config.gh_env() == {"GH_TOKEN": "env-token", "GITHUB_TOKEN": "env-token"}
    assert config.git_author_name == "Bot"


def test_configured_branch_context_builds_workspace_and_docs(tmp_path: Path, monkeypatch) -> None:
    agent_tasks_module = __import__("nyanpasu_github.agent_tasks", fromlist=["resolve_branch_sha"])
    doc = tmp_path / "AGENTS.md"
    doc.write_text("repo instructions\n", encoding="utf-8")
    seen_env: list[dict[str, str] | None] = []

    def resolve(*args, **kwargs):
        _ = args
        seen_env.append(kwargs.get("env"))
        return "base-sha"

    monkeypatch.setattr(agent_tasks_module, "resolve_branch_sha", resolve)
    monkeypatch.setenv("NYANPASU_TEST_GH_TOKEN", "token")

    context = configured_branch_context(
        repo="owner/repo",
        requested_base_branch=None,
        default_base_branch="main",
        repos={
            "owner/repo": GitHubRepoSettings(
                local_path=tmp_path / "repo",
                github_remote="git@example.com:owner/repo.git",
                base_branches=("main",),
                instruction_docs=(InstructionDocumentSettings(name="AGENTS.md", path=doc),),
            )
        },
        github=github_integration_from_config({"token_env": "NYANPASU_TEST_GH_TOKEN"}),
    )

    assert context.base_branch == "main"
    assert context.revision == "base-sha"
    assert context.workspace.ref == "refs/heads/main"
    assert context.workspace.revision == "base-sha"
    assert context.instruction_docs[0].content == "repo instructions\n"
    assert seen_env == [{"GH_TOKEN": "token", "GITHUB_TOKEN": "token"}]


def test_configured_branch_context_rejects_unknown_repo_and_branch(tmp_path: Path) -> None:
    repos = {
        "owner/repo": GitHubRepoSettings(
            local_path=tmp_path / "repo",
            base_branches=("develop",),
        )
    }

    with pytest.raises(GitHubRepoConfigError, match="repository is not configured"):
        configured_branch_context(
            repo="owner/missing",
            requested_base_branch=None,
            default_base_branch="develop",
            repos=repos,
        )
    with pytest.raises(GitHubRepoConfigError, match="base branch is not allowed"):
        configured_branch_context(
            repo="owner/repo",
            requested_base_branch="main",
            default_base_branch="develop",
            repos=repos,
        )


def test_branch_agent_task_uses_context_workspace(tmp_path: Path, monkeypatch) -> None:
    agent_tasks_module = __import__("nyanpasu_github.agent_tasks", fromlist=["resolve_branch_sha"])
    monkeypatch.setattr(agent_tasks_module, "resolve_branch_sha", lambda *_, **__: "base-sha")
    context = configured_branch_context(
        repo="owner/repo",
        requested_base_branch="main",
        default_base_branch="main",
        repos={"owner/repo": GitHubRepoSettings(local_path=tmp_path / "repo")},
    )

    task = branch_agent_task(
        task_id="task-1",
        context_key="ctx",
        prompt="do it",
        branch_context=context,
        metadata={"plugin_id": "demo"},
        dedupe_key="task-1",
    )

    assert task.action is TaskAction.RUN
    assert task.workspace == context.workspace
    assert task.metadata == {"plugin_id": "demo"}


def test_parse_pull_request_task_outcome() -> None:
    published = parse_pull_request_task_outcome("done\nPR: https://github.com/owner/repo/pull/12")
    no_changes = parse_pull_request_task_outcome("done\nNO_PR: already done")
    dry_run = parse_pull_request_task_outcome("dry", dry_run=True)
    failed = parse_pull_request_task_outcome("done")

    assert published.status == "published"
    assert published.pr_number == 12
    assert no_changes.status == "no_changes"
    assert no_changes.no_pr_reason == "already done"
    assert dry_run.status == "dry_run"
    assert failed.status == "failed"
    assert failed.error == "agent final message did not include `PR: <url>`"
