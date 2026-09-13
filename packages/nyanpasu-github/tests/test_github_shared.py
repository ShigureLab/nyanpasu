from __future__ import annotations

import hashlib
import hmac
import sys
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
    monkeypatch.setenv("NYANPASU_TEST_GH_TOKEN", "changed-token")
    assert config.resolved_token == "env-token"


def test_github_integration_command_resolves_once_in_config_directory(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "inherited")
    config = github_integration_from_config(
        {"token": {"cmd": [sys.executable, "-c", "open('count', 'a').write('1'); print('fixed-token')"]}},
        cwd=tmp_path,
    )
    monkeypatch.setenv("GH_TOKEN", "changed")
    assert config.gh_env() == {"GH_TOKEN": "fixed-token", "GITHUB_TOKEN": "fixed-token"}
    assert config.resolved_token == "fixed-token"
    assert (tmp_path / "count").read_text() == "1"
    assert "fixed-token" not in repr(config)


@pytest.mark.parametrize("settings", [{"token_env": "MISSING_NYANPASU_TOKEN"}, {"token": ""}])
def test_explicit_github_auth_never_falls_back_to_ambient(settings, monkeypatch) -> None:
    monkeypatch.delenv("MISSING_NYANPASU_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "unrelated-token")
    with pytest.raises(ValueError):
        github_integration_from_config(settings)


def test_github_integration_rejects_ambiguous_auth_without_executing_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="either token or token_env"):
        github_integration_from_config(
            {"token": {"cmd": ["touch", "unexpected"]}, "token_env": "GH_TOKEN"}, cwd=tmp_path
        )
    assert not (tmp_path / "unexpected").exists()


def test_github_integration_command_failure_hides_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GH_TOKEN", "ambient-token")
    with pytest.raises(ValueError, match="integrations.github.token: command exited with status 3") as error:
        github_integration_from_config(
            {"token": {"cmd": [sys.executable, "-c", "import sys; print('private-secret'); sys.exit(3)"]}},
            cwd=tmp_path,
        )
    assert "private-secret" not in str(error.value)


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
