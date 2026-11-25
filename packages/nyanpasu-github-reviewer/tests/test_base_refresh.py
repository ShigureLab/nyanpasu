from __future__ import annotations

import subprocess

import pytest

from nyanpasu.models import WorkspaceRef
from nyanpasu_github_reviewer.base_refresh import is_base_refresh
from nyanpasu_github_reviewer.models import PullRequestRef


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        ("base_only", True),
        ("rebase", True),
        ("rebase_shared_file", True),
        ("merge", True),
        ("merge_chain", True),
        ("conflict_resolution", False),
        ("extra_merge_change", False),
        ("local_commit", False),
        ("merge_then_local_commit", False),
    ],
)
def test_reviewed_base_refresh_with_real_git(tmp_path, update, expected):
    def git(*args, check=True):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=check)

    def commit(path, content):
        (tmp_path / path).write_text(content)
        git("add", path)
        git("commit", "-m", f"Update {path}")
        return git("rev-parse", "HEAD").stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Refresh fixture")
    git("config", "user.email", "fixture@example.com")
    git("config", "commit.gpgsign", "false")
    original = "".join(f"line {index}\n" for index in range(20))
    old_base = commit("shared.txt", original)
    git("checkout", "-b", "feature")
    old_head = commit("shared.txt", original.replace("line 18", "feature"))
    git("checkout", "main")
    if update in {"merge_chain", "rebase_shared_file"}:
        upstream = original.replace("line 1\n", "upstream\n")
        base = commit("shared.txt", upstream)
        if git("merge-tree", "--write-tree", old_head, base, check=False).returncode:
            pytest.skip("clean merge-tree is unavailable in this Git")
    elif update == "conflict_resolution":
        base = commit("shared.txt", original.replace("line 18", "upstream"))
    else:
        base = commit("upstream.txt", "upstream\n")
    git("checkout", "feature")
    if update in {"rebase", "rebase_shared_file"}:
        git("rebase", "main")
    elif update == "local_commit":
        commit("feature.txt", "new change\n")
    elif update == "conflict_resolution":
        assert git("merge", "--no-edit", "main", check=False).returncode == 1
        commit("shared.txt", original.replace("line 18", "resolved"))
    elif update == "extra_merge_change":
        git("merge", "--no-commit", "--no-ff", "main")
        commit("feature.txt", "extra merge change\n")
    elif update != "base_only":
        git("merge", "--no-edit", "main")
        if update == "merge_then_local_commit":
            commit("feature.txt", "new change\n")
        if update == "merge_chain":
            git("checkout", "main")
            base = commit("shared.txt", upstream.replace("line 4", "more upstream"))
            git("checkout", "feature")
            git("merge", "--no-edit", "main")

    head = git("rev-parse", "HEAD").stdout.strip()
    pr = PullRequestRef(
        repo="ExampleOrg/ExampleRepo",
        number=1,
        url="",
        base_ref="main",
        base_sha=base,
        head_ref="feature",
        head_sha=head,
        state="open",
        draft=False,
    )
    source = {"base_ref": "main", "head_sha": old_head, "merge_base_sha": old_base, "inventory_id": "reviewed"}
    workspace = WorkspaceRef(key=pr.repo, local_path=tmp_path)

    assert is_base_refresh(workspace, pr, source) is expected
    assert git("rev-parse", "HEAD").stdout.strip() == head
