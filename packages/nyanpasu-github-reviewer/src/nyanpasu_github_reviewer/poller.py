from __future__ import annotations

import asyncio
import functools
import json
import subprocess
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import anyio.to_thread as to_thread
from loguru import logger

from nyanpasu.git_ops import safe_slug
from nyanpasu_github_reviewer.events import parse_github_event
from nyanpasu_github_reviewer.models import (
    GitHubReviewerConfig,
    PollCycleResult,
    PollEventCursor,
    ReviewAction,
    ReviewEvent,
)

if TYPE_CHECKING:
    from nyanpasu_github_reviewer.store import GitHubReviewerStore


class PollAgent(Protocol):
    async def submit(self, event: ReviewEvent) -> dict[str, Any]: ...

    async def run_now(self, event: ReviewEvent) -> dict[str, Any]: ...


GhListRepoEvents = Callable[[GitHubReviewerConfig, str], list[dict[str, Any]]]


class GitHubEventsPoller:
    def __init__(
        self,
        config: GitHubReviewerConfig,
        *,
        store: GitHubReviewerStore | None = None,
        agent: PollAgent | None = None,
        event_status: Callable[[str], str | None] | None = None,
        list_repo_events: GhListRepoEvents | None = None,
    ) -> None:
        self.config = config
        if store is None:
            raise ValueError("GitHubReviewerStore is required")
        if agent is None:
            raise ValueError("PollAgent is required")
        self.store = store
        self.agent = agent
        self.event_status = event_status or (lambda _: None)
        self.list_repo_events = list_repo_events or list_repo_events_with_gh

    async def run_once(
        self,
        repos: Iterable[str] | None = None,
        *,
        wait_for_reviews: bool = False,
        force_baseline: bool = False,
    ) -> PollCycleResult:
        target_repos = tuple(repos or self.config.repos)
        started_at = time.monotonic()
        event_limit = _event_limit(self.config)
        logger.info(
            "events poll cycle started repos={} max_events={} wait_for_reviews={} force_baseline={}",
            ",".join(target_repos),
            event_limit if event_limit is not None else "unlimited",
            wait_for_reviews,
            force_baseline,
        )
        submitted = 0
        duplicates = 0
        ignored = 0
        baselined = 0
        remaining_events = event_limit
        for repo in target_repos:
            try:
                result = await self._poll_repo_events(
                    repo,
                    wait_for_reviews=wait_for_reviews,
                    force_baseline=force_baseline,
                    max_events=remaining_events,
                )
            except Exception:
                logger.exception("events poll repo failed repo={} action=skip_until_next_cycle", repo)
                continue
            logger.info(
                "events poll repo finished repo={} submitted={} duplicates={} ignored={} baselined={} remaining_events={}",
                repo,
                result.submitted,
                result.duplicates,
                result.ignored,
                result.baselined,
                _remaining_label(remaining_events, result.submitted),
            )
            submitted += result.submitted
            duplicates += result.duplicates
            ignored += result.ignored
            baselined += result.baselined
            if remaining_events is not None:
                remaining_events = max(remaining_events - result.submitted, 0)
        logger.info(
            "events poll cycle finished repos={} submitted={} duplicates={} ignored={} baselined={} elapsed_sec={:.2f}",
            ",".join(target_repos),
            submitted,
            duplicates,
            ignored,
            baselined,
            time.monotonic() - started_at,
        )
        return PollCycleResult(
            submitted=submitted,
            duplicates=duplicates,
            ignored=ignored,
            baselined=baselined,
            repos=target_repos,
        )

    async def run_forever(self, repos: Iterable[str] | None = None, interval_seconds: int | None = None) -> None:
        interval = interval_seconds or self.config.poll_interval_seconds
        target_repos = tuple(repos or self.config.repos)
        logger.info(
            "events poller started repos={} interval_sec={} event_pages={} max_events={} post_reviews={} dry_run={}",
            ",".join(target_repos),
            interval,
            self.config.poll_event_pages,
            _event_limit(self.config) if _event_limit(self.config) is not None else "unlimited",
            self.config.post_reviews,
            self.config.dry_run,
        )
        while True:
            await self.run_once(target_repos)
            logger.info("events poller sleeping interval_sec={}", interval)
            await asyncio.sleep(interval)

    async def shutdown(self) -> None:
        return None

    async def _poll_repo_events(
        self,
        repo: str,
        *,
        wait_for_reviews: bool,
        force_baseline: bool,
        max_events: int | None,
    ) -> PollCycleResult:
        logger.info(
            "events poll repo started repo={} max_events={}",
            repo,
            max_events if max_events is not None else "unlimited",
        )
        raw_events = await to_thread.run_sync(self.list_repo_events, self.config, repo)
        raw_events = _sorted_events(raw_events)
        cursor = await to_thread.run_sync(self.store.get_poll_event_cursor, repo)
        newest = _newest_cursor(raw_events)
        if newest is None:
            logger.info("events poll repo empty repo={}", repo)
            return PollCycleResult(submitted=0, duplicates=0, ignored=0, baselined=0, repos=(repo,))
        if force_baseline or cursor is None:
            await to_thread.run_sync(
                functools.partial(
                    self.store.upsert_poll_event_cursor,
                    repo,
                    last_event_created_at=newest.last_event_created_at,
                    cursor_event_ids=newest.cursor_event_ids,
                )
            )
            logger.info(
                "events poll repo baseline refreshed repo={} newest_created_at={} newest_event_count={} force_baseline={}",
                repo,
                newest.last_event_created_at,
                len(newest.cursor_event_ids),
                force_baseline,
            )
            return PollCycleResult(
                submitted=0,
                duplicates=0,
                ignored=len(raw_events),
                baselined=len(raw_events),
                repos=(repo,),
            )

        window = _events_after_cursor(raw_events, cursor)
        logger.info(
            "events poll repo listed repo={} fetched_events={} window_events={} since_created_at={} event_pages={}",
            repo,
            len(raw_events),
            len(window),
            cursor.last_event_created_at,
            self.config.poll_event_pages,
        )
        if not window:
            await to_thread.run_sync(
                functools.partial(
                    self.store.upsert_poll_event_cursor,
                    repo,
                    last_event_created_at=newest.last_event_created_at,
                    cursor_event_ids=newest.cursor_event_ids,
                )
            )
            return PollCycleResult(submitted=0, duplicates=0, ignored=len(raw_events), baselined=0, repos=(repo,))

        submitted = 0
        duplicates = 0
        ignored = 0
        processed_all_events = True
        for raw in window:
            delivery_id = _delivery_id_from_repo_event(repo, raw)
            event = event_from_repo_event(raw, delivery_id=delivery_id, agent_login=self.config.github_login)
            if event is None:
                ignored += 1
                continue
            if event.action is not ReviewAction.REVIEW and event.action is not ReviewAction.CLEANUP:
                ignored += 1
                continue
            if event.pr is not None and event.pr.repo not in self.config.repos:
                ignored += 1
                continue
            known_status = await to_thread.run_sync(self.event_status, event.delivery_id)
            if known_status is not None:
                duplicates += 1
                logger.info(
                    "events poll event skipped already processed repo={} delivery_id={} status={}",
                    repo,
                    event.delivery_id,
                    known_status,
                )
                continue
            if max_events is not None and submitted >= max_events:
                logger.warning("events poll repo event limit reached repo={} remaining_events_skipped=true", repo)
                processed_all_events = False
                break
            result = await self._dispatch_event(event, wait_for_reviews=wait_for_reviews)
            logger.info(
                "events poll event dispatched repo={} delivery_id={} github_event={} trigger={} result={}",
                repo,
                event.delivery_id,
                event.github_event,
                _event_trigger(event),
                _compact_result(result),
            )
            if result.get("duplicate"):
                duplicates += 1
            elif result.get("accepted"):
                submitted += 1

        if processed_all_events:
            await to_thread.run_sync(
                functools.partial(
                    self.store.upsert_poll_event_cursor,
                    repo,
                    last_event_created_at=newest.last_event_created_at,
                    cursor_event_ids=newest.cursor_event_ids,
                )
            )
        else:
            logger.info("events poll cursor not advanced repo={} reason=event_limit", repo)

        return PollCycleResult(submitted=submitted, duplicates=duplicates, ignored=ignored, baselined=0, repos=(repo,))

    async def _dispatch_event(self, event: ReviewEvent, *, wait_for_reviews: bool) -> dict[str, Any]:
        pr_label = f"{event.pr.repo}#{event.pr.number}" if event.pr else "<no-pr>"
        logger.info(
            "dispatching event delivery_id={} pr={} action={}",
            event.delivery_id,
            pr_label,
            event.action.value,
        )
        if wait_for_reviews:
            try:
                await self.agent.run_now(event)
            except ValueError as exc:
                if "duplicate" in str(exc):
                    logger.info("dispatch skipped duplicate delivery_id={} pr={}", event.delivery_id, pr_label)
                    return {"accepted": False, "duplicate": True, "delivery_id": event.delivery_id}
                raise
            return {"accepted": True, "delivery_id": event.delivery_id, "action": event.action.value}
        return await self.agent.submit(event)


def list_repo_events_with_gh(config: GitHubReviewerConfig, repo: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for page in range(1, config.poll_event_pages + 1):
        path = f"repos/{repo}/events?per_page=100&page={page}"
        proc = subprocess.run(
            ["gh", "api", "-X", "GET", path],
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            stdout = proc.stdout.strip()
            raise RuntimeError(
                f"gh repo events failed for {repo} page {page} with exit {proc.returncode}: "
                f"{stderr or stdout or 'no output'}"
            )
        data = json.loads(proc.stdout)
        if not isinstance(data, list):
            raise ValueError("gh repo events returned non-list JSON")
        events.extend(item for item in data if isinstance(item, dict))
        if len(data) < 100:
            break
    return events


def event_from_repo_event(
    raw_event: dict[str, Any],
    *,
    delivery_id: str,
    agent_login: str | None,
) -> ReviewEvent | None:
    event_type = str(raw_event.get("type") or "")
    payload_raw = raw_event.get("payload")
    if not isinstance(payload_raw, dict):
        return None
    payload = dict(payload_raw)
    repo_name = _repo_name(raw_event)
    if repo_name:
        payload.setdefault("repository", {"full_name": repo_name})
    payload["nyanpasu"] = _repo_event_context(raw_event)
    github_event = _github_event_name(event_type)
    if github_event is None:
        return None
    event = parse_github_event(github_event, delivery_id, payload, agent_login=agent_login)
    if event.action is ReviewAction.IGNORED:
        return event
    return event


def _repo_event_context(raw_event: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "events_poll",
        "repo_event_id": str(raw_event.get("id") or ""),
        "repo_event_type": str(raw_event.get("type") or ""),
        "repo_event_created_at": str(raw_event.get("created_at") or ""),
        "actor": str(raw_event.get("actor", {}).get("login") or "") if isinstance(raw_event.get("actor"), dict) else "",
    }


def _github_event_name(event_type: str) -> str | None:
    return {
        "IssueCommentEvent": "issue_comment",
        "PullRequestEvent": "pull_request",
        "PullRequestReviewCommentEvent": "pull_request_review_comment",
        "PullRequestReviewEvent": "pull_request_review",
    }.get(event_type)


def _repo_name(raw_event: dict[str, Any]) -> str:
    repo = raw_event.get("repo")
    if isinstance(repo, dict):
        return str(repo.get("name") or "")
    return ""


def _delivery_id_from_repo_event(repo: str, raw_event: dict[str, Any]) -> str:
    event_id = str(raw_event.get("id") or "")
    if event_id:
        return f"events-poll-{safe_slug(repo)}-{safe_slug(event_id)}"
    event_type = safe_slug(str(raw_event.get("type") or "event"))
    created_at = safe_slug(str(raw_event.get("created_at") or "unknown"))
    return f"events-poll-{safe_slug(repo)}-{event_type}-{created_at}"


def _sorted_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda item: (_event_created_at(item), _event_id(item)),
    )


def _events_after_cursor(events: list[dict[str, Any]], cursor: PollEventCursor) -> list[dict[str, Any]]:
    cursor_ids = set(cursor.cursor_event_ids)
    selected: list[dict[str, Any]] = []
    for event in events:
        created_at = _event_created_at(event)
        if created_at > cursor.last_event_created_at:
            selected.append(event)
            continue
        if created_at == cursor.last_event_created_at and _event_id(event) not in cursor_ids:
            selected.append(event)
    return selected


def _newest_cursor(events: list[dict[str, Any]]) -> PollEventCursor | None:
    if not events:
        return None
    newest_created_at = max(_event_created_at(event) for event in events)
    ids = tuple(
        _event_id(event) for event in events if _event_created_at(event) == newest_created_at and _event_id(event)
    )
    return PollEventCursor(
        repo="",
        last_event_created_at=newest_created_at,
        cursor_event_ids=ids,
        initialized_at=time.time(),
        updated_at=time.time(),
    )


def _event_created_at(event: dict[str, Any]) -> str:
    value = str(event.get("created_at") or "")
    parsed = _parse_github_timestamp(value)
    if parsed is None:
        return value
    return parsed.isoformat().replace("+00:00", "Z")


def _event_id(event: dict[str, Any]) -> str:
    return str(event.get("id") or "")


def _parse_github_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        try:
            return datetime.fromisoformat(value.removesuffix("Z") + "+00:00").astimezone(UTC)
        except ValueError:
            return None


def _event_limit(config: GitHubReviewerConfig) -> int | None:
    return config.poll_max_events_per_cycle if config.poll_max_events_per_cycle > 0 else None


def _remaining_label(remaining: int | None, submitted: int) -> str:
    if remaining is None:
        return "unlimited"
    return str(max(remaining - submitted, 0))


def _compact_result(result: dict[str, Any]) -> dict[str, Any]:
    keys = ("accepted", "duplicate", "ignored", "coalesced", "coalesced_into", "delivery_id", "task_id", "action")
    return {key: result[key] for key in keys if key in result}


def _event_trigger(event: ReviewEvent) -> str:
    context = event.raw.get("nyanpasu")
    if isinstance(context, dict) and context.get("trigger"):
        return str(context["trigger"])
    return str(event.raw.get("action", event.github_event))
