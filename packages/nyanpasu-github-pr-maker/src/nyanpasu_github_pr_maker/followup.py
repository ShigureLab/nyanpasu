from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from loguru import logger
from nyanpasu_github.pulls import PullRequestView, fetch_pull_request_view
from nyanpasu_github.workspace import branch_workspace_ref

from nyanpasu.models import AgentTask, TaskAction
from nyanpasu_github_pr_maker.models import PullRequestPublishMetadata
from nyanpasu_github_pr_maker.prompt import build_pr_follow_up_prompt

if TYPE_CHECKING:
    from nyanpasu.plugins import PluginRuntime
    from nyanpasu_github_pr_maker.models import GitHubPrMakerConfig
    from nyanpasu_github_pr_maker.store import GitHubPrMakerStore, ManagedPullRequestRecord


class GitHubPrMakerFollowUpPoller:
    def __init__(
        self,
        config: GitHubPrMakerConfig,
        *,
        store: GitHubPrMakerStore,
        runtime: PluginRuntime,
    ) -> None:
        self.config = config
        self.store = store
        self.runtime = runtime
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("github pr maker follow-up poll cycle failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.config.follow_up_interval_seconds)
                except TimeoutError:
                    pass
        finally:
            logger.info("github pr maker follow-up poller stopped")

    async def shutdown(self) -> None:
        self._stop.set()

    async def run_once(self) -> int:
        records = await asyncio.to_thread(self.store.list_active_managed_prs)
        submitted = 0
        for record in records:
            if record.repo not in self.config.repos:
                logger.info(
                    "github pr maker follow-up skipped unmanaged repo task_id={} repo={}",
                    record.task_id,
                    record.repo,
                )
                continue
            try:
                pr = await asyncio.to_thread(fetch_pull_request_view, record.repo, record.pr_number)
            except Exception:
                logger.exception(
                    "github pr maker follow-up fetch failed task_id={} repo={} pr={}",
                    record.task_id,
                    record.repo,
                    record.pr_number,
                )
                continue
            if not pr.is_open:
                await asyncio.to_thread(
                    self.store.update_managed_pr_cursor,
                    task_id=record.task_id,
                    last_digest=pr.follow_up_digest(),
                    last_head_sha=pr.head_sha,
                    active=False,
                )
                logger.info(
                    "github pr maker follow-up archived closed pr task_id={} repo={} pr={}",
                    record.task_id,
                    record.repo,
                    record.pr_number,
                )
                continue
            digest = pr.follow_up_digest()
            if record.last_digest == digest:
                logger.debug(
                    "github pr maker follow-up unchanged task_id={} repo={} pr={}",
                    record.task_id,
                    record.repo,
                    record.pr_number,
                )
                await asyncio.to_thread(
                    self.store.update_managed_pr_cursor,
                    task_id=record.task_id,
                    last_digest=digest,
                    last_head_sha=pr.head_sha,
                    active=True,
                )
                continue
            task = build_follow_up_task(self.config, record, pr)
            result = await self.runtime.submit(task)
            accepted = bool(result.get("accepted", False))
            submitted += int(accepted)
            logger.info(
                "github pr maker follow-up dispatched task_id={} follow_up_task_id={} repo={} pr={} accepted={} coalesced={}",
                record.task_id,
                task.task_id,
                record.repo,
                record.pr_number,
                accepted,
                bool(result.get("coalesced", False)),
            )
            await asyncio.to_thread(
                self.store.update_managed_pr_cursor,
                task_id=record.task_id,
                last_digest=digest,
                last_head_sha=pr.head_sha,
                active=True,
            )
        return submitted


def build_follow_up_task(
    config: GitHubPrMakerConfig,
    record: ManagedPullRequestRecord,
    pr: PullRequestView,
) -> AgentTask:
    repo_settings = config.repos[record.repo]
    task_id = f"{record.task_id}-followup-{int(time.time() * 1000)}"
    publish = PullRequestPublishMetadata(
        repo=record.repo,
        base_branch=record.base_branch,
        branch_name=record.branch_name,
        title=record.title,
        body=record.body,
        commit_message=f"Follow up {record.title}",
        task=record.task,
        context_key=record.context_key,
        labels=(),
        draft=False,
        dry_run=config.dry_run,
        existing_pr_number=record.pr_number,
        existing_pr_url=record.pr_url,
        remote_url=repo_settings.github_remote,
        git_author_name=config.git_author_name,
        git_author_email=config.git_author_email,
    )
    return AgentTask(
        task_id=task_id,
        action=TaskAction.RUN,
        context_key=record.context_key,
        prompt=build_pr_follow_up_prompt(config=config, record=record, pr=pr),
        workspace=branch_workspace_ref(
            repo=record.repo,
            settings=repo_settings,
            branch=record.branch_name,
            revision=pr.head_sha,
        ),
        dedupe_key=task_id,
        metadata={
            "plugin_id": "github_pr_maker",
            "follow_up": True,
            "managed_pr": _managed_pr_metadata(record, pr),
            "publish": publish.model_dump(mode="json"),
        },
    )


def _managed_pr_metadata(record: ManagedPullRequestRecord, pr: PullRequestView) -> dict[str, object]:
    return {
        "task_id": record.task_id,
        "repo": record.repo,
        "pr_number": record.pr_number,
        "pr_url": record.pr_url,
        "head_sha": pr.head_sha,
        "review_decision": pr.review_decision,
        "merge_state_status": pr.merge_state_status,
        "failing_checks": pr.failing_checks,
    }
