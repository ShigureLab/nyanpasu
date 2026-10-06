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
from pydantic import ConfigDict, RootModel

from nyanpasu.control_tools import EmptyInput, ToolSpec
from nyanpasu.git_ops import safe_slug
from nyanpasu.memory import MemoryAccess
from nyanpasu.models import AgentContext, AgentTask, SubtaskRequest, TaskAction, WorkspaceRef
from nyanpasu.store import StateStore
from nyanpasu_github_reviewer.ci import CISnapshot, fetch_ci_snapshot
from nyanpasu_github_reviewer.ci_tasks import ci_snapshot_data, prepare_ci_analysis, validate_ci_child
from nyanpasu_github_reviewer.events import event_dedupe_key, parse_github_event
from nyanpasu_github_reviewer.models import (
    GitHubReviewerConfig,
    PullRequestRef,
    ReviewAction,
    ReviewEvent,
    ReviewTrigger,
)
from nyanpasu_github_reviewer.poller import GitHubEventsPoller
from nyanpasu_github_reviewer.prompt import (
    INSTRUCTIONS_DIR,
    build_ci_followup_instructions,
    build_ci_followup_prompt,
    build_review_instructions,
    build_review_prompt,
    cleanup_prompt,
    review_trigger,
)
from nyanpasu_github_reviewer.reference import prepare_reference
from nyanpasu_github_reviewer.scope import (
    ScopePlan,
    admitted_files,
    build_inventory,
    review_source,
    scope_report,
    validate_plan,
)
from nyanpasu_github_reviewer.store import GitHubReviewerStore

if TYPE_CHECKING:
    from pydantic import BaseModel

    from nyanpasu.plugins import PluginRuntime

PLUGIN_ID = "github_reviewer"


class ReviewScopeInput(RootModel[ScopePlan | EmptyInput]):
    model_config = ConfigDict(hide_input_in_errors=True)


class GitHubReviewerPlugin:
    id = PLUGIN_ID
    config_model: type[BaseModel] | None = GitHubReviewerConfig

    def __init__(self, config: GitHubReviewerConfig | None = None) -> None:
        self.config = config
        self.runtime: PluginRuntime | None = None
        self.store: GitHubReviewerStore | None = None
        self.state_store: StateStore | None = None
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
        self.state_store = StateStore(runtime.config.db_path)
        runtime.add_task_preparer(self.id, self.prepare_task)
        runtime.add_subtask_preparer(self.id, self.prepare_subtask)
        runtime.add_task_control_tools(self.id, self.task_control_tools())
        # Plugin setup precedes task recovery and polling; reject old unscoped work
        # before any stored child can resume or be returned as an active retry.
        await asyncio.to_thread(self._validate_recovered_subtasks)
        runtime.add_router(
            self._router(),
            prefix="/plugins/github-reviewer",
            tags=["github-reviewer"],
            require_auth=not bool(config.webhook_secret),
        )
        if config.poll_enabled:
            poller = GitHubEventsPoller(
                config,
                store=self.store,
                agent=GitHubPollAgent(self),
                event_status=self.state_store.task_status,
            )
            self.poller_task = asyncio.create_task(poller.run_forever())
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
            kind="github_reviewer.review",
            memory=self.config.repos[pr.repo].memory_access(pr.repo)
            if pr and pr.repo in self.config.repos
            else MemoryAccess(),
            action=task_action,
            context_key=context_key,
            prompt=prompt,
            coalesce_key=context_key if task_action is TaskAction.RUN else None,
            workspace=workspace,
            dedupe_key=event_dedupe_key(event),
            metadata={
                "plugin_id": self.id,
                "pull_request": pr.model_dump(mode="json") if pr else None,
                "triggers": [review_trigger(event).model_dump(mode="json")],
            },
        )

    async def prepare_subtask(self, parent: AgentTask, request: SubtaskRequest) -> SubtaskRequest:
        if request.purpose == "ci-analysis":
            return await asyncio.to_thread(self._prepare_ci_subtask, parent, request)
        return await asyncio.to_thread(self._prepare_scoped_subtask, parent, request)

    def _prepare_ci_subtask(self, parent: AgentTask, request: SubtaskRequest) -> SubtaskRequest:
        assert self.config is not None
        if parent.spawned_by_task_id is not None:
            raise ValueError("CI analysis must be requested by the root reviewer")
        pr, snapshot = self._refresh_ci(parent)
        return prepare_ci_analysis(self.config, parent, request, pr, snapshot)

    @staticmethod
    def _scope_for_parent(store: StateStore, parent: AgentTask) -> tuple[dict, ScopePlan, set[str]]:
        root = store.task_request(store.root_task_id(parent.task_id))
        if "review_scope" not in root.metadata:
            raise ValueError(
                "submit a complete review-scope decision before creating deep-review subtasks; "
                "read " + str(INSTRUCTIONS_DIR / "scope-review.md")
            )
        inventory = root.metadata["review_inventory"]
        plan = validate_plan(inventory, root.metadata["review_scope"])
        allowed = admitted_files(plan)
        if parent.spawned_by_task_id is not None:
            assignment = parent.metadata.get("inputs", {}).get("review_scope")
            if assignment is None:
                raise ValueError("this child predates scope assignment; the root must dispatch new scoped work")
            allowed &= set(assignment["files"])
        return inventory, plan, allowed

    def _validate_recovered_subtasks(self) -> None:
        assert self.state_store is not None
        store = self.state_store
        for child in store.unfinished_tasks():
            if child.spawned_by_task_id is None or child.metadata.get("source_plugin_id") != self.id:
                continue
            if not store.task_is_active(child.task_id):
                continue  # An invalid ancestor already cancelled this branch.
            try:
                parent = store.task_request(child.spawned_by_task_id)
                if child.metadata.get("purpose") == "ci-analysis":
                    validate_ci_child(parent, child)
                    continue
                inventory, plan, allowed = self._scope_for_parent(store, parent)
                assignment = child.metadata.get("inputs", {}).get("review_scope")
                if (
                    assignment is None
                    or assignment["inventory_id"] != plan.inventory_id
                    or not assignment["files"]
                    or not set(assignment["files"]) <= allowed
                ):
                    raise ValueError("stored child has no valid accepted scope assignment")
                source = "merge_base_sha" if child.metadata.get("purpose") == "independent-design" else "head_sha"
                if child.workspace is None or child.workspace.revision != inventory[source]:
                    raise ValueError("stored child revision does not match its scope inventory")
            except ValueError as exc:
                error = (
                    f"CI analysis cannot resume: {exc}. Refresh CI and dispatch with a new request_key."
                    if child.metadata.get("purpose") == "ci-analysis"
                    else f"Deep review cannot resume: {exc}. Submit review-scope and dispatch with a new request_key."
                )
                store.mark_task_failed(child.task_id, error)
                logger.warning("review child recovery rejected task_id={} reason={}", child.task_id, error)

    def _prepare_scoped_subtask(self, parent: AgentTask, request: SubtaskRequest) -> SubtaskRequest:
        assert self.runtime is not None
        assert self.state_store is not None
        inventory, plan, allowed = self._scope_for_parent(self.state_store, parent)
        inputs = dict(request.inputs)
        files = inputs.pop("review_files", None)
        if not isinstance(files, list) or not files or not all(isinstance(path, str) for path in files):
            raise ValueError("inputs.review_files must list the accepted changed files owned by this child")
        if len(files) != len(set(files)) or not set(files) <= allowed:
            raise ValueError("child scope must be unique accepted files within its parent's responsibility")
        if request.purpose != "independent-design" and request.revision not in {None, inventory["head_sha"]}:
            raise ValueError("deep review must use the inventory head")
        prepared = prepare_reference(self.runtime.config, parent, request.model_copy(update={"inputs": inputs}))
        if request.purpose != "independent-design":
            prepared = prepared.model_copy(
                update={
                    "revision": inventory["head_sha"],
                    "developer_instructions": prepared.developer_instructions
                    + "\nOwned changed files (JSON data): "
                    + json.dumps(files)
                    + f"\nPath encoding: {inventory.get('path_encoding', 'utf-8')}. "
                    "For percent encoding, decode with urllib.parse.unquote_to_bytes before filesystem access; "
                    "keep the original tokens in scope reports."
                    + "\nRead other files for context when needed; report on your owned scope. "
                    "Do not expand into deferred evidence, demos or other unassigned review work.",
                }
            )
        # The independent designer's prompt contains requirements only, not author paths.
        return prepared.model_copy(
            update={
                "kind": request.kind if request.kind != "subtask" else f"github_reviewer.{request.purpose}",
                "memory_enabled": request.memory_enabled and request.purpose != "independent-design",
                "inputs": {
                    **prepared.inputs,
                    "review_scope": {"inventory_id": plan.inventory_id, "files": files},
                },
            }
        )

    def task_control_tools(self) -> tuple[ToolSpec[Any], ...]:
        return (
            ToolSpec(
                "review-scope",
                "Root reviewer: omit input to read the pinned Git inventory and current scope; "
                "submit a complete plan covering each inventory path once to set the admission scope. "
                "Use exact inventory path tokens. Scope freezes after review child dispatch.",
                ReviewScopeInput,
                self.review_scope_control,
                self._root_tool_available,
            ),
            ToolSpec(
                "review-verify",
                "Root reviewer: immediately before review publication, verify current PR eligibility and the pinned "
                "head, base ref and merge-base. This does not verify unchanged requirements or discussions.",
                EmptyInput,
                self.review_verify_control,
                self._root_tool_available,
            ),
            ToolSpec(
                "ci-refresh",
                "Root reviewer: refresh current PR eligibility, head, CI failure instances and snapshot fingerprint "
                "before acting or publishing CI evidence. A failed refresh does not establish recovery.",
                EmptyInput,
                self.ci_refresh_control,
                self._root_tool_available,
            ),
        )

    @staticmethod
    def _root_tool_available(task: AgentTask) -> bool:
        return task.spawned_by_task_id is None

    async def review_scope_control(self, task: AgentTask, request: ReviewScopeInput) -> dict:
        return await asyncio.to_thread(self._scope_control, task, request.root.model_dump())

    async def review_verify_control(self, task: AgentTask, request: EmptyInput) -> dict:
        return await asyncio.to_thread(self._verify_review, task)

    async def ci_refresh_control(self, task: AgentTask, request: EmptyInput) -> dict:
        pr, snapshot = await asyncio.to_thread(self._refresh_ci, task)
        return {"pull_request": pr.model_dump(mode="json"), "snapshot": ci_snapshot_data(snapshot)}

    def _refresh_ci(self, task: AgentTask) -> tuple[PullRequestRef, CISnapshot]:
        assert self.config is not None
        pinned_pr = PullRequestRef.model_validate(task.metadata["pull_request"])
        pr = _fetch_pr(self.config, pinned_pr.repo, pinned_pr.number)
        if pr.state != "open" or pr.draft or not self._repo_allows_base_branch(pr):
            raise ValueError("PR is no longer eligible for CI analysis or publication")
        return pr, fetch_ci_snapshot(self.config, pr)

    def _verify_review(self, task: AgentTask) -> dict:
        assert self.config is not None
        assert self.runtime is not None
        inventory = task.metadata.get("review_inventory")
        if inventory is None:
            raise ValueError("read review-scope before verifying the review")
        queued_pr = PullRequestRef.model_validate(task.metadata["pull_request"])
        pr = _fetch_pr(self.config, queued_pr.repo, queued_pr.number)
        if pr.state != "open" or pr.draft or not self._repo_allows_base_branch(pr):
            raise ValueError("PR is no longer eligible for review publication")
        current = build_inventory(
            self.runtime.config,
            task.model_copy(
                update={
                    "workspace": self._workspace_for_pr(pr),
                    "metadata": {**task.metadata, "pull_request": pr.model_dump(mode="json")},
                }
            ),
        )
        if inventory["inventory_id"] != current["inventory_id"]:
            raise ValueError(
                "review range changed; do not publish or reuse this run's conclusions; start a new review run"
            )
        return {"source": review_source(current)}

    def _scope_control(self, task: AgentTask, payload: dict[str, Any]) -> dict:
        assert self.runtime is not None
        assert self.state_store is not None
        store = self.state_store
        # A recovered pre-upgrade root obtains its inventory before continuing.
        inventory = task.metadata.get("review_inventory") or build_inventory(self.runtime.config, task)
        if "base_ref" not in inventory:
            raise ValueError("review inventory predates range tracking; start a new review run")
        task = task.model_copy(update={"metadata": {**task.metadata, "review_inventory": inventory}})
        if payload:
            plan = validate_plan(inventory, payload)
            previous = task.metadata.get("review_scope")
            scoped_children = any(
                child.metadata.get("purpose") != "ci-analysis"
                for child in (store.task_request(record.task_id) for record in store.subtasks(task.task_id))
            )
            if previous is not None and scoped_children and plan.model_dump() != previous:
                raise ValueError("scope is frozen after child dispatch; reconsider it in a new review run")
            task = task.model_copy(update={"metadata": {**task.metadata, "review_scope": plan.model_dump()}})
        store.update_task_input(task)
        plan_data = task.metadata.get("review_scope")
        report = scope_report(inventory, validate_plan(inventory, plan_data)) if plan_data is not None else None
        result = {"source": review_source(inventory), "scope": report}
        return result if payload else {"inventory": inventory, **result}

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
        metadata: dict[str, Any] = {
            **task.metadata,
            "pull_request": pr.model_dump(mode="json"),
            "triggers": [item.model_dump(mode="json") for item in triggers],
            "memory_query": "\n".join([pr.head_ref, *(trigger.body for trigger in triggers)]),
        }
        if pr.state != "open" or pr.draft or not self._repo_allows_base_branch(pr):
            return task.model_copy(
                update={
                    "action": TaskAction.IGNORED,
                    "prompt": f"Review skipped: {pr.url} is no longer an eligible open PR.",
                    "metadata": metadata,
                }
            )
        workspace = self._workspace_for_pr(pr)
        assert task.execution is not None
        if triggers and all(trigger.kind == "ci_changed" for trigger in triggers):
            snapshot = ci_snapshot_data(fetch_ci_snapshot(self.config, pr))
            metadata["ci_snapshot"] = snapshot
            return task.model_copy(
                update={
                    "workspace": workspace,
                    "developer_instructions": build_ci_followup_instructions(self.config, pr),
                    "instruction_docs": instruction_documents_for_repo(
                        repo=pr.repo,
                        plugin_instruction_docs=self.config.instruction_docs,
                        repo_settings=self.config.repos,
                    ),
                    "prompt": build_ci_followup_prompt(
                        self.config,
                        pr,
                        runtime=task.execution,
                        snapshot=snapshot,
                        has_session=bool(context and context.thread_id),
                    ),
                    "metadata": metadata,
                }
            )
        metadata["review_inventory"] = build_inventory(
            self.runtime.config, task.model_copy(update={"workspace": workspace, "metadata": metadata})
        )
        metadata["memory_query"] += "\n" + "\n".join(item["path"] for item in metadata["review_inventory"]["files"])
        if metadata.get("review_scope", {}).get("inventory_id") != metadata["review_inventory"]["inventory_id"]:
            metadata.pop("review_scope", None)
        return task.model_copy(
            update={
                "workspace": workspace,
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
                    runtime=task.execution,
                    triggers=triggers,
                    has_session=bool(context and context.thread_id),
                    previous_task_head=context.revision if context else None,
                    inventory=metadata["review_inventory"],
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
        if event.pr.repo not in self.config.repos:
            return event.model_copy(update={"action": ReviewAction.IGNORED})
        event = self._hydrate_review_event_pr(event)
        pr = event.pr
        if pr is None:
            return event
        if not self._repo_allows_base_branch(pr):
            return event.model_copy(update={"action": ReviewAction.IGNORED})
        if not self._review_thread_event_is_relevant(event):
            return event.model_copy(update={"action": ReviewAction.IGNORED})
        return event

    def _hydrate_review_event_pr(self, event: ReviewEvent) -> ReviewEvent:
        if event.pr is None or event.action is not ReviewAction.REVIEW:
            return event
        if (
            event.pr.head_sha
            and event.pr.base_ref
            and event.pr.head_ref
            and (event.pr.stack is not None or self._repo_allows_base_branch(event.pr))
        ):
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
        if self.config.github_login and _mentions_login(review_trigger(event).body, self.config.github_login):
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
        return pr.targets_any(repo_config.base_branches)


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
    data = gh_json(["api", f"repos/{repo}/pulls/{pr_number}"], env=config.gh_env)
    return PullRequestRef.from_github(repo, data)


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
