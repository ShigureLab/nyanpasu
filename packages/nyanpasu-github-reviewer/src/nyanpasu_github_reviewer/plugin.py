from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request
from loguru import logger
from nyanpasu_github.gh import GitHubCommandError, GitHubSignatureError, gh_json, run_gh, verify_webhook_signature
from nyanpasu_github.instructions import instruction_documents_for_repo
from nyanpasu_github.models import GitHubIntegrationConfig, github_integration_from_config
from nyanpasu_github.workspace import pull_request_workspace_ref

from nyanpasu.git_ops import safe_slug
from nyanpasu.models import AgentContext, AgentTask, TaskAction, WorkspaceRef
from nyanpasu.store import StateStore
from nyanpasu_github_reviewer.events import parse_github_event
from nyanpasu_github_reviewer.models import (
    GitHubReviewerConfig,
    PullRequestRef,
    ReviewAction,
    ReviewEvent,
    ReviewTrigger,
)
from nyanpasu_github_reviewer.poller import GitHubEventsPoller
from nyanpasu_github_reviewer.prompt import (
    build_review_instructions,
    build_review_prompt,
    cleanup_prompt,
    review_trigger,
)
from nyanpasu_github_reviewer.store import GitHubReviewerStore

if TYPE_CHECKING:
    from pydantic import BaseModel

    from nyanpasu.plugins import PluginRuntime

PLUGIN_ID = "github_reviewer"


class GitHubReviewerPlugin:
    id = PLUGIN_ID
    config_model: type[BaseModel] | None = GitHubReviewerConfig

    def __init__(self, config: GitHubReviewerConfig | None = None) -> None:
        self.config = config
        self.runtime: PluginRuntime | None = None
        self.store: GitHubReviewerStore | None = None
        self.poller: GitHubEventsPoller | None = None
        self.poller_task: asyncio.Task[None] | None = None
        self.github: GitHubIntegrationConfig = GitHubIntegrationConfig()

    async def setup(self, runtime: PluginRuntime, config: BaseModel | dict[str, Any]) -> None:
        if not isinstance(config, GitHubReviewerConfig):
            config = GitHubReviewerConfig.model_validate(config)
        self.github = github_integration_from_config(
            runtime.config.integrations.get("github"), cwd=runtime.config.state_dir
        )
        config = config.model_copy(update={"gh_env": self.github.gh_env()})
        self.config = config
        self.runtime = runtime
        self.store = GitHubReviewerStore(runtime.config.db_path)
        runtime.add_task_preparer(self.id, self.prepare_task)
        runtime.add_router(self._router(), prefix="/plugins/github-reviewer", tags=["github-reviewer"])
        if config.poll_enabled:
            self.poller = GitHubEventsPoller(
                config,
                store=self.store,
                agent=GitHubPollAgent(self),
                event_status=lambda delivery_id: StateStore(runtime.config.db_path).task_status(delivery_id),
            )
            self.poller_task = asyncio.create_task(self.poller.run_forever())
            logger.info(
                "github reviewer poller started repos={} interval_sec={}",
                ",".join(config.repos),
                config.poll_interval_seconds,
            )

    async def shutdown(self) -> None:
        if self.poller_task is not None:
            self.poller_task.cancel()
            try:
                await self.poller_task
            except asyncio.CancelledError:
                pass
        if self.poller is not None:
            await self.poller.shutdown()
        logger.info("github reviewer plugin shutdown finished")

    def _router(self) -> APIRouter:
        router = APIRouter()

        @router.post("/webhook", status_code=202)
        async def webhook(
            request: Request,
            x_github_event: Annotated[str | None, Header(alias="X-GitHub-Event")] = None,
            x_github_delivery: Annotated[str | None, Header(alias="X-GitHub-Delivery")] = None,
            x_hub_signature_256: Annotated[str | None, Header(alias="X-Hub-Signature-256")] = None,
        ) -> dict[str, Any]:
            if self.config is None or self.runtime is None:
                raise HTTPException(status_code=503, detail="github reviewer plugin is not initialized")
            body = await request.body()
            verify_signature(body, x_hub_signature_256, self.config.webhook_secret)
            github_event = x_github_event or ""
            delivery_id = x_github_delivery or hashlib.sha256(body).hexdigest()
            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail=f"invalid JSON payload: {exc}") from exc
            event = parse_github_event(github_event, delivery_id, payload, agent_login=self.config.github_login)
            task = self.event_to_task(event)
            result = await self.runtime.submit(task)
            return result

        return router

    def event_to_task(self, event: ReviewEvent) -> AgentTask:
        if self.config is None:
            raise RuntimeError("github reviewer plugin is not initialized")
        event = self._preflight_event(event)
        task_action = _task_action(event.action)
        pr = event.pr
        context_key = f"github:{pr.repo}#{pr.number}" if pr else f"github:event:{event.delivery_id}"
        workspace = self._workspace_for_pr(pr) if pr else None
        prompt = f"Review {pr.repo} PR #{pr.number}." if pr else ""
        if task_action is TaskAction.CLEANUP and pr is not None:
            prompt = cleanup_prompt(pr)
        return AgentTask(
            task_id=event.delivery_id,
            action=task_action,
            context_key=context_key,
            prompt=prompt,
            coalesce_key=context_key if task_action is TaskAction.RUN else None,
            workspace=workspace,
            dedupe_key=event.delivery_id,
            metadata={
                "plugin_id": self.id,
                "pull_request": pr.model_dump(mode="json") if pr else None,
                "triggers": [review_trigger(event).model_dump(mode="json")],
            },
            cleanup_policy="context" if task_action is TaskAction.CLEANUP else "none",
        )

    async def prepare_task(
        self, task: AgentTask, coalesced: tuple[AgentTask, ...], context: AgentContext | None
    ) -> AgentTask:
        return await asyncio.to_thread(self._prepare_review, task, coalesced, context)

    def _prepare_review(
        self, task: AgentTask, coalesced: tuple[AgentTask, ...], context: AgentContext | None
    ) -> AgentTask:
        assert self.config is not None
        assert self.runtime is not None
        queued_pr = PullRequestRef.model_validate(task.metadata["pull_request"])
        pr = _fetch_pr(self.config, queued_pr.repo, queued_pr.number)
        triggers = tuple(
            ReviewTrigger.model_validate(trigger)
            for item in (task, *coalesced)
            for trigger in item.metadata["triggers"]
        )
        metadata = {
            **task.metadata,
            "pull_request": pr.model_dump(mode="json"),
            "triggers": [item.model_dump(mode="json") for item in triggers],
        }
        if pr.state != "open" or pr.draft or not self._repo_allows_base_branch(pr):
            return task.model_copy(
                update={
                    "action": TaskAction.IGNORED,
                    "prompt": f"Review skipped: {pr.url} is no longer an eligible open PR.",
                    "metadata": metadata,
                }
            )
        return task.model_copy(
            update={
                "workspace": self._workspace_for_pr(pr),
                "developer_instructions": build_review_instructions(self.config, pr),
                "instruction_docs": instruction_documents_for_repo(
                    repo=pr.repo,
                    plugin_instruction_docs=self.config.instruction_docs,
                    repo_settings=self.config.repos,
                ),
                "prompt": build_review_prompt(
                    self.config,
                    pr,
                    "{{NYANPASU_WORKTREE}}",
                    codex=self.runtime.config.codex,
                    triggers=triggers,
                    has_session=bool(context and context.thread_id),
                    previous_task_head=context.revision if context else None,
                ),
                "metadata": metadata,
            }
        )

    def _workspace_for_pr(self, pr: PullRequestRef | None) -> WorkspaceRef | None:
        if pr is None or self.config is None:
            return None
        repo_settings = self.config.repos.get(pr.repo)
        if repo_settings is None:
            return None
        return pull_request_workspace_ref(pr, repo_settings)

    def _preflight_event(self, event: ReviewEvent) -> ReviewEvent:
        if self.config is None or event.pr is None or event.action is not ReviewAction.REVIEW:
            return event
        event = self._hydrate_review_event_pr(event)
        pr = event.pr
        if pr is None:
            return event
        if pr.repo not in self.config.repos:
            return event.model_copy(update={"action": ReviewAction.IGNORED})
        if not self._repo_allows_base_branch(pr):
            return event.model_copy(update={"action": ReviewAction.IGNORED})
        if not self._review_thread_event_is_relevant(event):
            return event.model_copy(update={"action": ReviewAction.IGNORED})
        return event

    def _hydrate_review_event_pr(self, event: ReviewEvent) -> ReviewEvent:
        if event.pr is None or event.action is not ReviewAction.REVIEW:
            return event
        if event.pr.head_sha and event.pr.base_ref and event.pr.head_ref:
            return event
        assert self.config is not None
        hydrated = _fetch_pr(self.config, event.pr.repo, event.pr.number)
        return event.model_copy(update={"pr": hydrated, "after_sha": event.after_sha or hydrated.head_sha})

    def _review_thread_event_is_relevant(self, event: ReviewEvent) -> bool:
        if self.config is None:
            return True
        context = event.raw.get("nyanpasu")
        if not isinstance(context, dict) or context.get("trigger") != "review_thread_comment":
            return True
        body_excerpt = str(context.get("body_excerpt") or "")
        if self.config.github_login and _mentions_login(body_excerpt, self.config.github_login):
            return True
        parent_comment_id = context.get("in_reply_to_id")
        if parent_comment_id is None or event.pr is None or not self.config.github_login:
            return False
        try:
            proc = run_gh(
                [
                    "api",
                    f"repos/{event.pr.repo}/pulls/comments/{parent_comment_id}",
                    "--jq",
                    ".user.login",
                ],
                env=self.config.gh_env,
                timeout=30,
            )
        except (OSError, GitHubCommandError):
            return False
        parent_author = proc.stdout.strip()
        return parent_author.casefold() == self.config.github_login.casefold()

    def _repo_allows_base_branch(self, pr: PullRequestRef) -> bool:
        if self.config is None:
            return True
        repo_config = self.config.repo_configs.get(pr.repo)
        if repo_config is None or not repo_config.base_branches:
            return True
        return pr.base_ref in repo_config.base_branches


def verify_signature(body: bytes, signature: str | None, secret: str | None) -> None:
    try:
        verify_webhook_signature(body, signature, secret)
    except GitHubSignatureError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _task_action(action: ReviewAction) -> TaskAction:
    if action is ReviewAction.REVIEW:
        return TaskAction.RUN
    if action is ReviewAction.CLEANUP:
        return TaskAction.CLEANUP
    return TaskAction.IGNORED


def _mentions_login(text: str, login: str) -> bool:
    import re

    pattern = rf"(?<![A-Za-z0-9-])@{re.escape(login)}(?![A-Za-z0-9-])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def manual_event_task(config: GitHubReviewerConfig, repo: str, pr_number: int) -> AgentTask:
    pr = _fetch_pr(config, repo, pr_number)
    event = ReviewEvent(
        delivery_id=f"manual-{safe_slug(repo)}-{pr_number}-{time.time_ns()}",
        github_event="manual_review",
        action=ReviewAction.REVIEW,
        pr=pr,
        after_sha=pr.head_sha,
        raw={"nyanpasu": {"trigger": "manual_review", "trigger_summary": "Review explicitly requested from the CLI."}},
    )
    return GitHubReviewerPlugin(config).event_to_task(event)


def _fetch_pr(config: GitHubReviewerConfig, repo: str, pr_number: int) -> PullRequestRef:
    fields = "number,state,isDraft,url,baseRefName,headRefName,headRefOid"
    data = gh_json(["pr", "view", str(pr_number), "--repo", repo, "--json", fields], env=config.gh_env)
    return PullRequestRef(
        repo=repo,
        number=int(data["number"]),
        url=str(data["url"]),
        state=str(data["state"]).lower(),
        draft=bool(data["isDraft"]),
        base_ref=str(data["baseRefName"]),
        head_ref=str(data["headRefName"]),
        head_sha=str(data["headRefOid"]),
    )


def plugin() -> GitHubReviewerPlugin:
    return GitHubReviewerPlugin()


class GitHubPollAgent:
    def __init__(self, plugin: GitHubReviewerPlugin) -> None:
        self.plugin = plugin

    async def submit(self, event: ReviewEvent) -> dict[str, Any]:
        if self.plugin.runtime is None:
            raise RuntimeError("github reviewer plugin is not initialized")
        return await self.plugin.runtime.submit(self.plugin.event_to_task(event))

    async def run_now(self, event: ReviewEvent) -> dict[str, Any]:
        if self.plugin.runtime is None:
            raise RuntimeError("github reviewer plugin is not initialized")
        result = await self.plugin.runtime.run_now(self.plugin.event_to_task(event))
        return {"accepted": True, "task_id": result.task_id, "action": event.action.value}
