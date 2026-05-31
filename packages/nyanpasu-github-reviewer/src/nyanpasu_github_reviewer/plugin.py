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
from nyanpasu.models import AgentContext, AgentTask, InstructionDocument, TaskAction, WorkspaceRef
from nyanpasu.store import StateStore
from nyanpasu_github_reviewer.events import parse_github_event
from nyanpasu_github_reviewer.models import (
    GitHubReviewerConfig,
    PullRequestRef,
    ReviewAction,
    ReviewEvent,
)
from nyanpasu_github_reviewer.poller import GitHubEventsPoller
from nyanpasu_github_reviewer.prompt import build_review_prompt, cleanup_prompt
from nyanpasu_github_reviewer.store import GitHubReviewerStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from nyanpasu.plugins import PluginRuntime

PLUGIN_ID = "github_reviewer"


class GitHubReviewerPlugin:
    id = PLUGIN_ID
    config_model: type[BaseModel] | None = GitHubReviewerConfig

    def __init__(self) -> None:
        self.config: GitHubReviewerConfig | None = None
        self.runtime: PluginRuntime | None = None
        self.store: GitHubReviewerStore | None = None
        self.poller: GitHubEventsPoller | None = None
        self.poller_task: asyncio.Task[None] | None = None
        self.github: GitHubIntegrationConfig = GitHubIntegrationConfig()

    async def setup(self, runtime: PluginRuntime, config: BaseModel | dict[str, Any]) -> None:
        if not isinstance(config, GitHubReviewerConfig):
            config = GitHubReviewerConfig.model_validate(config)
        self.github = github_integration_from_config(runtime.config.integrations.get("github"))
        config = config.model_copy(update={"gh_env": self.github.gh_env()})
        self.config = config
        self.runtime = runtime
        self.store = GitHubReviewerStore(runtime.config.db_path)
        runtime.add_router(self._router(), prefix="/plugins/github-reviewer", tags=["github-reviewer"])
        runtime.add_post_process_hook(self.id, self._post_process)
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

    def bind_for_conversion(
        self,
        *,
        config: GitHubReviewerConfig,
        context_lookup: Callable[[str], AgentContext | None] | None = None,
    ) -> GitHubReviewerPlugin:
        self.config = config
        self._context_lookup = context_lookup
        return self

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

    async def _post_process(self, task: AgentTask, result) -> None:
        _ = task, result

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
        prompt = ""
        review_mode = "initial_review"
        previous_head_sha = None
        instruction_docs: tuple[InstructionDocument, ...] = ()
        if task_action is TaskAction.RUN:
            if pr is None:
                raise ValueError("review event has no pull request")
            instruction_docs = self._instruction_docs_for_pr(pr)
            existing = self.runtime_store_context(context_key)
            review_mode = "initial_review" if existing is None else "followup_review"
            previous_head_sha = existing.get("revision") if existing else None
            event = self._with_coalesced_event_context(event)
            prompt = build_review_prompt(
                self.config,
                event,
                "{{NYANPASU_WORKTREE}}",
                review_mode=review_mode,
                previous_head_sha=previous_head_sha,
            )
        elif task_action is TaskAction.CLEANUP and pr is not None:
            prompt = cleanup_prompt(pr)
        return AgentTask(
            task_id=event.delivery_id,
            action=task_action,
            context_key=context_key,
            prompt=prompt,
            workspace=workspace,
            instruction_docs=instruction_docs,
            dedupe_key=event.delivery_id,
            metadata={
                "plugin_id": self.id,
                "github_event": event.github_event,
                "pull_request": event.pr.model_dump(mode="json") if event.pr else None,
                "delivery_id": event.delivery_id,
                "review_mode": review_mode,
                "previous_head_sha": previous_head_sha,
                "raw": event.raw,
            },
            cleanup_policy="context" if task_action is TaskAction.CLEANUP else "none",
        )

    def _instruction_docs_for_pr(self, pr: PullRequestRef):
        if self.config is None:
            return ()
        return instruction_documents_for_repo(
            repo=pr.repo,
            plugin_instruction_docs=self.config.instruction_docs,
            repo_settings=self.config.repos,
        )

    def runtime_store_context(self, context_key: str) -> dict[str, Any] | None:
        context_lookup = getattr(self, "_context_lookup", None)
        if context_lookup is not None:
            context = context_lookup(context_key)
            if context is None:
                return None
            return {
                "thread_id": context.thread_id,
                "session_worktree": str(context.session_worktree) if context.session_worktree else None,
                "workspace_key": context.workspace_key,
                "revision": context.revision,
            }
        if self.runtime is None:
            return None
        context = StateStore(self.runtime.config.db_path).get_context(context_key)
        if context is None:
            return None
        return {
            "thread_id": context.thread_id,
            "session_worktree": str(context.session_worktree) if context.session_worktree else None,
            "workspace_key": context.workspace_key,
            "revision": context.revision,
        }

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
        data = gh_json(
            [
                "pr",
                "view",
                str(event.pr.number),
                "--repo",
                event.pr.repo,
                "--json",
                "number,state,isDraft,url,baseRefName,headRefName,headRefOid",
            ],
            env=self.config.gh_env if self.config else None,
        )
        hydrated = PullRequestRef(
            repo=event.pr.repo,
            number=int(data["number"]),
            url=str(data.get("url") or event.pr.url),
            base_ref=str(data.get("baseRefName") or event.pr.base_ref),
            head_ref=str(data.get("headRefName") or event.pr.head_ref),
            head_sha=str(data.get("headRefOid") or event.pr.head_sha),
            state=str(data.get("state") or event.pr.state).lower(),
            draft=bool(data.get("isDraft", event.pr.draft)),
        )
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

    def _with_coalesced_event_context(self, event: ReviewEvent) -> ReviewEvent:
        _ = event
        return event


def event_with_coalesced_tasks(event: ReviewEvent, coalesced_tasks: list[dict[str, Any]]) -> ReviewEvent:
    raw = dict(event.raw)
    context = raw.get("nyanpasu")
    nyanpasu = dict(context) if isinstance(context, dict) else {}
    nyanpasu["coalesced_events"] = coalesced_tasks
    raw["nyanpasu"] = nyanpasu
    return event.model_copy(update={"raw": raw})


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
    payload = _pr_payload_from_gh(config, repo, pr_number)
    event = parse_github_event(
        "pull_request",
        f"manual-{safe_slug(repo)}-{pr_number}-{int(time.time())}",
        payload,
        agent_login=config.github_login,
    )
    plugin = GitHubReviewerPlugin()
    plugin.bind_for_conversion(config=config)
    return plugin.event_to_task(event)


def _pr_payload_from_gh(config: GitHubReviewerConfig, repo: str, pr: int) -> dict[str, object]:
    fields = "number,state,isDraft,url,baseRefName,headRefName,headRefOid"
    data = gh_json(["pr", "view", str(pr), "--repo", repo, "--json", fields], env=config.gh_env)
    return {
        "action": "synchronize",
        "repository": {"full_name": repo},
        "pull_request": {
            "number": data["number"],
            "html_url": data["url"],
            "state": str(data["state"]).lower(),
            "draft": data["isDraft"],
            "base": {"ref": data["baseRefName"]},
            "head": {"ref": data["headRefName"], "sha": data["headRefOid"]},
        },
    }


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
