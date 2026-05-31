from __future__ import annotations

import hashlib
import hmac
from typing import TYPE_CHECKING

import pytest
from nyanpasu_github.gh import GitHubSignatureError, verify_webhook_signature
from nyanpasu_github.instructions import instruction_documents_for_repo
from nyanpasu_github.models import GitHubRepoSettings, InstructionDocumentSettings, PullRequestRef
from nyanpasu_github.workspace import branch_workspace_ref, pull_request_workspace_ref

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
