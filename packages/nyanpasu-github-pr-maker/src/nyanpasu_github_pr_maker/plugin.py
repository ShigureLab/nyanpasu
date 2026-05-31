from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from loguru import logger
from nyanpasu_github.gh import resolve_branch_sha
from nyanpasu_github.instructions import instruction_documents_for_repo
from nyanpasu_github.publisher import (
    GitHubPullRequestPublisher,
    PublishPullRequestRequest,
    PullRequestPublisher,
)
from nyanpasu_github.pulls import fetch_pull_request_view
from nyanpasu_github.workspace import branch_workspace_ref

from nyanpasu.git_ops import safe_slug
from nyanpasu.models import AgentTask, TaskAction
from nyanpasu_github_pr_maker.followup import GitHubPrMakerFollowUpPoller
from nyanpasu_github_pr_maker.models import (
    CreatePullRequestTaskRequest,
    GitHubPrMakerConfig,
    PullRequestPublishMetadata,
)
from nyanpasu_github_pr_maker.prompt import build_pr_maker_prompt
from nyanpasu_github_pr_maker.store import GitHubPrMakerStore

if TYPE_CHECKING:
    from pydantic import BaseModel

    from nyanpasu.models import TaskRunResult
    from nyanpasu.plugins import PluginRuntime

PLUGIN_ID = "github_pr_maker"


class GitHubPrMakerPlugin:
    id = PLUGIN_ID
    config_model: type[BaseModel] | None = GitHubPrMakerConfig

    def __init__(self, *, publisher: PullRequestPublisher | None = None) -> None:
        self.config: GitHubPrMakerConfig | None = None
        self.runtime: PluginRuntime | None = None
        self.store: GitHubPrMakerStore | None = None
        self.publisher = publisher or GitHubPullRequestPublisher()
        self.follow_up_poller: GitHubPrMakerFollowUpPoller | None = None
        self.follow_up_task: asyncio.Task[None] | None = None

    async def setup(self, runtime: PluginRuntime, config: BaseModel | dict[str, Any]) -> None:
        if not isinstance(config, GitHubPrMakerConfig):
            config = GitHubPrMakerConfig.model_validate(config)
        self.config = config
        self.runtime = runtime
        self.store = GitHubPrMakerStore(runtime.config.db_path)
        runtime.add_router(self._router(), prefix="/plugins/github-pr-maker", tags=["github-pr-maker"])
        runtime.add_post_process_hook(self.id, self._post_process)
        if config.follow_up_enabled:
            self.follow_up_poller = GitHubPrMakerFollowUpPoller(config, store=self.store, runtime=runtime)
            self.follow_up_task = asyncio.create_task(self.follow_up_poller.run_forever())
        logger.info("github pr maker plugin started repos={}", ",".join(config.repos))

    async def shutdown(self) -> None:
        if self.follow_up_task is not None:
            if self.follow_up_poller is not None:
                await self.follow_up_poller.shutdown()
            self.follow_up_task.cancel()
            try:
                await self.follow_up_task
            except asyncio.CancelledError:
                pass
        logger.info("github pr maker plugin shutdown finished")

    def _router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/tasks", status_code=202)
        async def create_task(request: CreatePullRequestTaskRequest) -> dict[str, Any]:
            if self.runtime is None:
                raise HTTPException(status_code=503, detail="github pr maker plugin is not initialized")
            task = self.request_to_task(request)
            return await self.runtime.submit(task)

        @router.get("/tasks/{task_id}")
        async def task_result(task_id: str) -> dict[str, Any]:
            if self.store is None:
                raise HTTPException(status_code=503, detail="github pr maker plugin is not initialized")
            record = self.store.get(task_id)
            if record is None:
                raise HTTPException(status_code=404, detail="task result is not available")
            return record.model_dump(mode="json")

        return router

    def request_to_task(self, request: CreatePullRequestTaskRequest) -> AgentTask:
        if self.config is None:
            raise RuntimeError("github pr maker plugin is not initialized")
        repo_settings = self.config.repos.get(request.repo)
        if repo_settings is None:
            raise HTTPException(status_code=400, detail=f"repository is not configured: {request.repo}")
        base_branch = request.base_branch or self.config.default_base_branch
        if repo_settings.base_branches and base_branch not in repo_settings.base_branches:
            raise HTTPException(status_code=400, detail=f"base branch is not allowed: {base_branch}")
        revision = resolve_branch_sha(request.repo, base_branch)
        task_id = request.task_id or _task_id(request.repo)
        branch_name = request.branch_name or _branch_name(self.config.branch_prefix, task_id)
        title = request.title or _title_from_task(request.task)
        body = request.body or _default_pr_body(request.task)
        commit_message = request.commit_message or title
        context_key = request.context_key or f"github-pr-maker:{request.repo}:{task_id}"
        dry_run = self.config.dry_run if request.dry_run is None else request.dry_run
        draft = self.config.draft if request.draft is None else request.draft
        publish = PullRequestPublishMetadata(
            repo=request.repo,
            base_branch=base_branch,
            branch_name=branch_name,
            title=title,
            body=body,
            commit_message=commit_message,
            task=request.task,
            context_key=context_key,
            labels=request.labels,
            draft=draft,
            dry_run=dry_run,
            remote_url=repo_settings.github_remote,
            git_author_name=self.config.git_author_name,
            git_author_email=self.config.git_author_email,
        )
        instruction_docs = instruction_documents_for_repo(
            repo=request.repo,
            plugin_instruction_docs=self.config.instruction_docs,
            repo_settings=self.config.repos,
        )
        return AgentTask(
            task_id=task_id,
            action=TaskAction.RUN,
            context_key=context_key,
            prompt=build_pr_maker_prompt(config=self.config, request=request, base_branch=base_branch),
            workspace=branch_workspace_ref(
                repo=request.repo,
                settings=repo_settings,
                branch=base_branch,
                revision=revision,
            ),
            instruction_docs=instruction_docs,
            dedupe_key=task_id,
            metadata={
                "plugin_id": self.id,
                "request": request.model_dump(mode="json"),
                "publish": publish.model_dump(mode="json"),
            },
        )

    async def _post_process(self, task: AgentTask, result: TaskRunResult) -> None:
        if self.store is None:
            raise RuntimeError("github pr maker plugin is not initialized")
        publish_raw = task.metadata.get("publish")
        if not isinstance(publish_raw, dict):
            return
        publish = PullRequestPublishMetadata.model_validate(publish_raw)
        if result.session_worktree is None:
            self.store.upsert_result(
                task_id=task.task_id,
                repo=publish.repo,
                branch_name=publish.branch_name,
                status="failed",
                pr_url=publish.existing_pr_url,
                pr_number=publish.existing_pr_number,
                error="task completed without a session worktree",
                result={},
            )
            return
        try:
            pr_result = self.publisher.publish(
                worktree=result.session_worktree,
                request=PublishPullRequestRequest(**publish.model_dump()),
            )
        except Exception as exc:
            self.store.upsert_result(
                task_id=task.task_id,
                repo=publish.repo,
                branch_name=publish.branch_name,
                status="failed",
                pr_url=publish.existing_pr_url,
                pr_number=publish.existing_pr_number,
                result={},
                error=str(exc),
            )
            logger.exception("github pr maker publish failed task_id={} repo={}", task.task_id, publish.repo)
            return
        self.store.upsert_result(
            task_id=task.task_id,
            repo=publish.repo,
            branch_name=publish.branch_name,
            status=pr_result.status,
            pr_url=pr_result.pr_url,
            pr_number=pr_result.pr_number,
            result=pr_result.model_dump(mode="json"),
        )
        if (
            pr_result.status == "published"
            and publish.existing_pr_number is None
            and pr_result.pr_url is not None
            and pr_result.pr_number is not None
        ):
            self._register_managed_pr(publish, pr_number=pr_result.pr_number, pr_url=pr_result.pr_url)
        logger.info(
            "github pr maker publish finished task_id={} repo={} status={} pr_url={} follow_up={}",
            task.task_id,
            publish.repo,
            pr_result.status,
            pr_result.pr_url or "",
            bool(pr_result.pr_number and self.config and self.config.follow_up_enabled),
        )

    def _register_managed_pr(self, publish: PullRequestPublishMetadata, *, pr_number: int, pr_url: str) -> None:
        if self.config is None or self.store is None:
            raise RuntimeError("github pr maker plugin is not initialized")
        if not self.config.follow_up_enabled:
            return
        pr = fetch_pull_request_view(publish.repo, pr_number)
        self.store.upsert_managed_pr(
            task_id=_managed_pr_task_id(publish.repo, pr_number),
            context_key=publish.context_key,
            repo=publish.repo,
            pr_number=pr_number,
            pr_url=pr_url,
            base_branch=publish.base_branch,
            branch_name=publish.branch_name,
            title=publish.title,
            body=publish.body,
            task=publish.task,
            last_digest=pr.follow_up_digest(),
            last_head_sha=pr.head_sha,
        )
        logger.info(
            "github pr maker registered follow-up pr repo={} pr={} context={} head={}",
            publish.repo,
            pr_number,
            publish.context_key,
            pr.head_sha,
        )


def _task_id(repo: str) -> str:
    return f"pr-maker-{safe_slug(repo)}-{int(time.time() * 1000)}"


def _branch_name(prefix: str, task_id: str) -> str:
    return f"{safe_slug(prefix)}/{safe_slug(task_id)}"


def _title_from_task(task: str) -> str:
    first = " ".join(task.strip().splitlines()[0].split())
    return first[:80] if first else "Implement requested change"


def _default_pr_body(task: str) -> str:
    return f"Generated by Nyanpasu from task request:\n\n{task.strip()}\n"


def _managed_pr_task_id(repo: str, pr_number: int) -> str:
    return f"managed-pr-{safe_slug(repo)}-{pr_number}"


def plugin() -> GitHubPrMakerPlugin:
    return GitHubPrMakerPlugin()
