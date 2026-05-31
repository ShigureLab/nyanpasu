from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

import anyio.to_thread as to_thread
from loguru import logger
from nyanpasu_github.gh import run_gh

from nyanpasu.git_ops import safe_slug
from nyanpasu_github_reviewer.events import parse_github_event
from nyanpasu_github_reviewer.models import (
    GitHubEventJournalRecord,
    GitHubEventJournalStatus,
    GitHubReviewerConfig,
    PollCycleResult,
    PollEventCursor,
    PullRequestSnapshot,
    PullRequestTimelineCursor,
    PullRequestUpdatedCursor,
    ReviewAction,
    ReviewEvent,
)

if TYPE_CHECKING:
    from nyanpasu_github_reviewer.store import GitHubReviewerStore


class PollAgent(Protocol):
    async def submit(self, event: ReviewEvent) -> dict[str, Any]: ...

    async def run_now(self, event: ReviewEvent) -> dict[str, Any]: ...


GhListRepoEvents = Callable[[GitHubReviewerConfig, str], list[dict[str, Any]]]
GhListPullRequests = Callable[[GitHubReviewerConfig, str], list[dict[str, Any]]]
GhListPullRequestTimeline = Callable[[GitHubReviewerConfig, str, int], list[dict[str, Any]]]


class GitHubEventsPoller:
    def __init__(
        self,
        config: GitHubReviewerConfig,
        *,
        store: GitHubReviewerStore | None = None,
        agent: PollAgent | None = None,
        event_status: Callable[[str], str | None] | None = None,
        list_repo_events: GhListRepoEvents | None = None,
        list_pull_requests: GhListPullRequests | None = None,
        list_pull_request_timeline: GhListPullRequestTimeline | None = None,
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
        self.list_pull_requests = list_pull_requests or list_pull_requests_with_gh
        self.list_pull_request_timeline = list_pull_request_timeline or list_pull_request_timeline_with_gh

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
                discovery = await self._poll_repo(
                    repo,
                    force_baseline=force_baseline,
                )
                result = await self._dispatch_pending_repo_events(
                    repo,
                    wait_for_reviews=wait_for_reviews,
                    max_events=remaining_events,
                )
                result = result.model_copy(
                    update={
                        "duplicates": result.duplicates + discovery.duplicates,
                        "ignored": result.ignored + discovery.ignored,
                        "baselined": result.baselined + discovery.baselined,
                    }
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

    async def _poll_repo(
        self,
        repo: str,
        *,
        force_baseline: bool,
    ) -> PollCycleResult:
        repo_events = await self._poll_repo_events(repo, force_baseline=force_baseline)
        pr_state = await self._poll_pr_state(repo, force_baseline=force_baseline)
        return PollCycleResult(
            submitted=0,
            duplicates=repo_events.duplicates + pr_state.duplicates,
            ignored=repo_events.ignored + pr_state.ignored,
            baselined=repo_events.baselined + pr_state.baselined,
            repos=(repo,),
        )

    async def _poll_repo_events(
        self,
        repo: str,
        *,
        force_baseline: bool,
    ) -> PollCycleResult:
        logger.info(
            "events poll repo started repo={}",
            repo,
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
            _log_source_summary(
                source="repo_events_poll",
                repo=repo,
                fetched=len(raw_events),
                window=0,
                journaled=0,
                duplicates=0,
                ignored=len(raw_events),
                baselined=len(raw_events),
                ignored_reasons=Counter({"baseline": len(raw_events)}),
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
            _log_source_summary(
                source="repo_events_poll",
                repo=repo,
                fetched=len(raw_events),
                window=0,
                journaled=0,
                duplicates=0,
                ignored=len(raw_events),
                baselined=0,
                ignored_reasons=Counter({"before_cursor": len(raw_events)}),
            )
            return PollCycleResult(submitted=0, duplicates=0, ignored=len(raw_events), baselined=0, repos=(repo,))

        duplicates = 0
        ignored = 0
        ignored_reasons: Counter[str] = Counter()
        for raw in window:
            delivery_id = _delivery_id_from_repo_event(repo, raw)
            event = event_from_repo_event(raw, delivery_id=delivery_id, agent_login=self.config.github_login)
            if event is None:
                ignored += 1
                reason = _repo_event_ignore_reason(raw, event)
                ignored_reasons[reason] += 1
                _log_poll_decision(
                    source="repo_events_poll",
                    decision="ignored",
                    reason=reason,
                    repo=repo,
                    raw=raw,
                    delivery_id=delivery_id,
                )
                continue
            if event.action is not ReviewAction.REVIEW and event.action is not ReviewAction.CLEANUP:
                ignored += 1
                reason = _repo_event_ignore_reason(raw, event)
                ignored_reasons[reason] += 1
                _log_poll_decision(
                    source="repo_events_poll",
                    decision="ignored",
                    reason=reason,
                    repo=repo,
                    raw=raw,
                    event=event,
                    delivery_id=delivery_id,
                )
                continue
            if event.pr is not None and event.pr.repo not in self.config.repos:
                ignored += 1
                reason = "unconfigured_repo"
                ignored_reasons[reason] += 1
                _log_poll_decision(
                    source="repo_events_poll",
                    decision="ignored",
                    reason=reason,
                    repo=repo,
                    raw=raw,
                    event=event,
                    delivery_id=delivery_id,
                )
                continue
            record = _journal_record_from_event(
                event, source="repo_events_poll", event_created_at=_event_created_at(raw)
            )
            inserted = await to_thread.run_sync(self.store.append_event, record)
            if not inserted:
                duplicates += 1
                _log_poll_decision(
                    source="repo_events_poll",
                    decision="duplicate",
                    reason="already_journaled",
                    repo=repo,
                    raw=raw,
                    event=event,
                    record=record,
                )
                continue
            _log_poll_decision(
                source="repo_events_poll",
                decision="journaled",
                repo=repo,
                raw=raw,
                event=event,
                record=record,
            )

        await to_thread.run_sync(
            functools.partial(
                self.store.upsert_poll_event_cursor,
                repo,
                last_event_created_at=newest.last_event_created_at,
                cursor_event_ids=newest.cursor_event_ids,
            )
        )
        _log_source_summary(
            source="repo_events_poll",
            repo=repo,
            fetched=len(raw_events),
            window=len(window),
            journaled=len(window) - ignored - duplicates,
            duplicates=duplicates,
            ignored=ignored,
            baselined=0,
            ignored_reasons=ignored_reasons,
        )

        return PollCycleResult(submitted=0, duplicates=duplicates, ignored=ignored, baselined=0, repos=(repo,))

    async def _poll_pr_state(self, repo: str, *, force_baseline: bool) -> PollCycleResult:
        logger.info("pr state poll repo started repo={}", repo)
        raw_prs = await to_thread.run_sync(self.list_pull_requests, self.config, repo)
        snapshots = [_snapshot_from_pr(repo, raw) for raw in raw_prs]
        snapshots = sorted(snapshots, key=lambda item: (item.updated_at, item.node_id))
        newest = _newest_pr_updated_cursor(repo, snapshots)
        cursor = await to_thread.run_sync(self.store.get_pr_updated_cursor, repo)
        if newest is None:
            logger.info("pr state poll repo empty repo={}", repo)
            return PollCycleResult(submitted=0, duplicates=0, ignored=0, baselined=0, repos=(repo,))
        if force_baseline or cursor is None:
            for snapshot in snapshots:
                await to_thread.run_sync(self.store.upsert_pr_snapshot, snapshot)
            await to_thread.run_sync(
                functools.partial(
                    self.store.upsert_pr_updated_cursor,
                    repo,
                    last_updated_at=newest.last_updated_at,
                    pr_node_ids=newest.pr_node_ids,
                )
            )
            logger.info(
                "pr state poll baseline refreshed repo={} newest_updated_at={} prs={}",
                repo,
                newest.last_updated_at,
                len(snapshots),
            )
            _log_source_summary(
                source="pr_state_poll",
                repo=repo,
                fetched=len(snapshots),
                window=0,
                journaled=0,
                duplicates=0,
                ignored=len(snapshots),
                baselined=len(snapshots),
                ignored_reasons=Counter({"baseline": len(snapshots)}),
            )
            return PollCycleResult(
                submitted=0,
                duplicates=0,
                ignored=len(snapshots),
                baselined=len(snapshots),
                repos=(repo,),
            )

        window = _prs_after_updated_cursor(snapshots, cursor.last_updated_at, set(cursor.pr_node_ids))
        logger.info(
            "pr state poll repo listed repo={} fetched_prs={} window_prs={} since_updated_at={}",
            repo,
            len(snapshots),
            len(window),
            cursor.last_updated_at,
        )
        duplicates = 0
        ignored = 0
        ignored_reasons: Counter[str] = Counter()
        timeline_result = PollCycleResult(submitted=0, duplicates=0, ignored=0, baselined=0, repos=(repo,))
        for snapshot in window:
            previous = await to_thread.run_sync(self.store.get_pr_snapshot, repo, snapshot.number)
            reason = _snapshot_ignore_reason(
                snapshot,
                previous,
                first_seen_after=cursor.last_updated_at,
            )
            event = _event_from_snapshot_diff(
                snapshot,
                previous,
                agent_login=self.config.github_login,
                first_seen_after=cursor.last_updated_at,
            )
            await to_thread.run_sync(self.store.upsert_pr_snapshot, snapshot)
            if event is None:
                ignored += 1
                ignored_reasons[reason] += 1
                _log_poll_decision(
                    source="pr_state_poll",
                    decision="ignored",
                    reason=reason,
                    repo=repo,
                    pr_number=snapshot.number,
                    event_created_at=snapshot.updated_at,
                    head_sha=snapshot.head_sha,
                )
                continue
            record = _journal_record_from_event(event, source="pr_state_poll", event_created_at=snapshot.updated_at)
            inserted = await to_thread.run_sync(self.store.append_event, record)
            if not inserted:
                duplicates += 1
                _log_poll_decision(
                    source="pr_state_poll",
                    decision="duplicate",
                    reason="already_journaled",
                    repo=repo,
                    pr_number=snapshot.number,
                    event=event,
                    record=record,
                    head_sha=snapshot.head_sha,
                )
                continue
            _log_poll_decision(
                source="pr_state_poll",
                decision="journaled",
                repo=repo,
                pr_number=snapshot.number,
                event=event,
                record=record,
                head_sha=snapshot.head_sha,
            )
        timeline_result = await self._poll_pr_timelines(
            repo,
            window,
            first_seen_after=cursor.last_updated_at,
        )
        await to_thread.run_sync(
            functools.partial(
                self.store.upsert_pr_updated_cursor,
                repo,
                last_updated_at=newest.last_updated_at,
                pr_node_ids=newest.pr_node_ids,
            )
        )
        _log_source_summary(
            source="pr_state_poll",
            repo=repo,
            fetched=len(snapshots),
            window=len(window),
            journaled=len(window) - ignored - duplicates,
            duplicates=duplicates,
            ignored=ignored,
            baselined=0,
            ignored_reasons=ignored_reasons,
        )
        return PollCycleResult(
            submitted=0,
            duplicates=duplicates + timeline_result.duplicates,
            ignored=ignored + timeline_result.ignored,
            baselined=timeline_result.baselined,
            repos=(repo,),
        )

    async def _poll_pr_timelines(
        self,
        repo: str,
        snapshots: list[PullRequestSnapshot],
        *,
        first_seen_after: str,
    ) -> PollCycleResult:
        duplicates = 0
        ignored = 0
        baselined = 0
        total_items = 0
        total_window = 0
        ignored_reasons: Counter[str] = Counter()
        candidates = [snapshot for snapshot in snapshots if snapshot.state == "open" and not snapshot.draft]
        for snapshot in candidates:
            logger.info("pr timeline poll started repo={} pr={}", repo, snapshot.number)
            raw_items = await to_thread.run_sync(
                self.list_pull_request_timeline,
                self.config,
                repo,
                snapshot.number,
            )
            items = _sorted_timeline_items(raw_items)
            total_items += len(items)
            newest = _newest_timeline_cursor(repo, snapshot.number, items)
            if newest is None:
                logger.info("pr timeline poll empty repo={} pr={}", repo, snapshot.number)
                continue
            cursor = await to_thread.run_sync(self.store.get_pr_timeline_cursor, repo, snapshot.number)
            if cursor is None:
                window = _timeline_items_strictly_after(items, first_seen_after)
            else:
                window = _timeline_items_after(items, cursor.last_item_updated_at, set(cursor.item_ids))
            logger.info(
                "pr timeline poll listed repo={} pr={} fetched_items={} window_items={} since_updated_at={}",
                repo,
                snapshot.number,
                len(items),
                len(window),
                cursor.last_item_updated_at if cursor is not None else first_seen_after,
            )
            if cursor is None and not window:
                baselined += len(items)
                _log_source_summary(
                    source="pr_timeline_poll",
                    repo=repo,
                    pr_number=snapshot.number,
                    fetched=len(items),
                    window=0,
                    journaled=0,
                    duplicates=0,
                    ignored=len(items),
                    baselined=len(items),
                    ignored_reasons=Counter({"baseline": len(items)}),
                )
            total_window += len(window)
            pr_duplicates = 0
            pr_ignored = 0
            pr_ignored_reasons: Counter[str] = Counter()
            for raw in window:
                if _timeline_item_is_from_agent(raw, self.config.github_login):
                    ignored += 1
                    pr_ignored += 1
                    reason = "agent_authored_item"
                    ignored_reasons[reason] += 1
                    pr_ignored_reasons[reason] += 1
                    _log_poll_decision(
                        source="pr_timeline_poll",
                        decision="ignored",
                        reason=reason,
                        repo=repo,
                        pr_number=snapshot.number,
                        raw=raw,
                    )
                    continue
                item_updated_at = _timeline_item_updated_at(raw)
                delivery_id = _delivery_id_from_timeline_item(repo, snapshot.number, raw)
                event = event_from_pr_timeline_item(
                    raw,
                    snapshot,
                    delivery_id=delivery_id,
                    agent_login=self.config.github_login,
                )
                if event is None or event.action is not ReviewAction.REVIEW:
                    ignored += 1
                    pr_ignored += 1
                    reason = _timeline_item_ignore_reason(raw, event)
                    ignored_reasons[reason] += 1
                    pr_ignored_reasons[reason] += 1
                    _log_poll_decision(
                        source="pr_timeline_poll",
                        decision="ignored",
                        reason=reason,
                        repo=repo,
                        pr_number=snapshot.number,
                        raw=raw,
                        event=event,
                        delivery_id=delivery_id,
                    )
                    continue
                record = _journal_record_from_event(event, source="pr_timeline_poll", event_created_at=item_updated_at)
                inserted = await to_thread.run_sync(self.store.append_event, record)
                if not inserted:
                    duplicates += 1
                    pr_duplicates += 1
                    _log_poll_decision(
                        source="pr_timeline_poll",
                        decision="duplicate",
                        reason="already_journaled",
                        repo=repo,
                        pr_number=snapshot.number,
                        raw=raw,
                        event=event,
                        record=record,
                    )
                    continue
                _log_poll_decision(
                    source="pr_timeline_poll",
                    decision="journaled",
                    repo=repo,
                    pr_number=snapshot.number,
                    raw=raw,
                    event=event,
                    record=record,
                )
            if window:
                _log_source_summary(
                    source="pr_timeline_poll",
                    repo=repo,
                    pr_number=snapshot.number,
                    fetched=len(items),
                    window=len(window),
                    journaled=len(window) - pr_ignored - pr_duplicates,
                    duplicates=pr_duplicates,
                    ignored=pr_ignored,
                    baselined=0,
                    ignored_reasons=pr_ignored_reasons,
                )
            await to_thread.run_sync(
                functools.partial(
                    self.store.upsert_pr_timeline_cursor,
                    repo,
                    snapshot.number,
                    last_item_updated_at=_timeline_cursor_timestamp(newest, first_seen_after)
                    if cursor is None
                    else newest.last_item_updated_at,
                    item_ids=_timeline_cursor_item_ids(newest, items, first_seen_after)
                    if cursor is None
                    else newest.item_ids,
                )
            )
        _log_source_summary(
            source="pr_timeline_poll",
            repo=repo,
            fetched=total_items,
            window=total_window,
            journaled=total_window - ignored - duplicates,
            duplicates=duplicates,
            ignored=ignored,
            baselined=baselined,
            ignored_reasons=ignored_reasons,
        )
        return PollCycleResult(
            submitted=0,
            duplicates=duplicates,
            ignored=ignored,
            baselined=baselined,
            repos=(repo,),
        )

    async def _dispatch_pending_repo_events(
        self,
        repo: str,
        *,
        wait_for_reviews: bool,
        max_events: int | None,
    ) -> PollCycleResult:
        pending = await to_thread.run_sync(functools.partial(self.store.pending_events, repo=repo))
        logger.info(
            "journal dispatch queue repo={} pending={} max_events={} wait_for_reviews={}",
            repo,
            len(pending),
            max_events if max_events is not None else "unlimited",
            wait_for_reviews,
        )
        submitted = 0
        duplicates = 0
        ignored = 0
        for record in pending:
            _log_journal_decision(record, "pending", reason="queued_for_dispatch")
            known_status = await to_thread.run_sync(self.event_status, record.delivery_id)
            if known_status is not None:
                duplicates += 1
                await to_thread.run_sync(
                    functools.partial(
                        self.store.mark_event_status,
                        record.delivery_id,
                        GitHubEventJournalStatus.SKIPPED,
                        result_json=json.dumps({"duplicate_task_status": known_status}, ensure_ascii=False),
                    )
                )
                _log_journal_decision(
                    record, "skipped", reason="already_processed", result={"task_status": known_status}
                )
                continue
            if max_events is not None and submitted >= max_events:
                logger.info(
                    "journal dispatch event limit reached repo={} submitted={} remaining_pending=true",
                    repo,
                    submitted,
                )
                _log_journal_decision(record, "pending", reason="event_limit_reached")
                break
            await to_thread.run_sync(
                self.store.mark_event_status,
                record.delivery_id,
                GitHubEventJournalStatus.RUNNING,
            )
            _log_journal_decision(record, "running", reason="dispatch_started")
            try:
                result = await self._dispatch_event(record.event, wait_for_reviews=wait_for_reviews)
            except Exception as exc:
                await to_thread.run_sync(
                    functools.partial(
                        self.store.mark_event_status,
                        record.delivery_id,
                        GitHubEventJournalStatus.FAILED,
                        error=str(exc),
                    )
                )
                _log_journal_decision(record, "failed", reason="dispatch_error", error=str(exc))
                raise
            status = GitHubEventJournalStatus.COMPLETED
            if result.get("duplicate") or result.get("ignored"):
                status = GitHubEventJournalStatus.SKIPPED
            await to_thread.run_sync(
                functools.partial(
                    self.store.mark_event_status,
                    record.delivery_id,
                    status,
                    result_json=json.dumps(_compact_result(result), ensure_ascii=False),
                )
            )
            _log_journal_decision(record, status.value, reason="dispatch_finished", result=_compact_result(result))
            if result.get("duplicate"):
                duplicates += 1
            elif result.get("ignored"):
                ignored += 1
            elif result.get("accepted"):
                submitted += 1
        logger.info(
            "journal dispatch finished repo={} submitted={} duplicates={} ignored={} pending_seen={}",
            repo,
            submitted,
            duplicates,
            ignored,
            len(pending),
        )
        return PollCycleResult(submitted=submitted, duplicates=duplicates, ignored=ignored, baselined=0, repos=(repo,))

    async def _dispatch_event(self, event: ReviewEvent, *, wait_for_reviews: bool) -> dict[str, Any]:
        pr_label = f"{event.pr.repo}#{event.pr.number}" if event.pr else "<no-pr>"
        logger.info(
            "agent dispatch started delivery_id={} pr={} github_event={} trigger={} action={} wait_for_reviews={}",
            event.delivery_id,
            pr_label,
            event.github_event,
            _event_trigger(event),
            event.action.value,
            wait_for_reviews,
        )
        if wait_for_reviews:
            try:
                await self.agent.run_now(event)
            except ValueError as exc:
                if "duplicate" in str(exc):
                    logger.info(
                        "agent dispatch skipped delivery_id={} pr={} reason=duplicate_task",
                        event.delivery_id,
                        pr_label,
                    )
                    return {"accepted": False, "duplicate": True, "delivery_id": event.delivery_id}
                raise
            return {"accepted": True, "delivery_id": event.delivery_id, "action": event.action.value}
        result = await self.agent.submit(event)
        logger.info(
            "agent dispatch accepted delivery_id={} pr={} result={}",
            event.delivery_id,
            pr_label,
            _compact_result(result),
        )
        return result


def list_repo_events_with_gh(config: GitHubReviewerConfig, repo: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for page in range(1, config.poll_event_pages + 1):
        path = f"repos/{repo}/events?per_page=100&page={page}"
        proc = run_gh(["api", "-X", "GET", path], env=config.gh_env)
        data = json.loads(proc.stdout)
        if not isinstance(data, list):
            raise ValueError("gh repo events returned non-list JSON")
        events.extend(item for item in data if isinstance(item, dict))
        if len(data) < 100:
            break
    return events


def list_pull_requests_with_gh(config: GitHubReviewerConfig, repo: str) -> list[dict[str, Any]]:
    pulls: list[dict[str, Any]] = []
    repo_settings = config.repos.get(repo)
    base_branches = repo_settings.base_branches if repo_settings is not None else ()
    bases = base_branches or (None,)
    for base in bases:
        for page in range(1, config.poll_event_pages + 1):
            path = f"repos/{repo}/pulls?state=all&sort=updated&direction=desc&per_page=100&page={page}"
            if base:
                path += f"&base={base}"
            proc = run_gh(["api", "-X", "GET", path], env=config.gh_env)
            data = json.loads(proc.stdout)
            if not isinstance(data, list):
                raise ValueError("gh pulls returned non-list JSON")
            pulls.extend(item for item in data if isinstance(item, dict))
            if len(data) < 100:
                break
    return pulls


def list_pull_request_timeline_with_gh(
    config: GitHubReviewerConfig,
    repo: str,
    pr_number: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, config.poll_event_pages + 1):
        path = f"repos/{repo}/issues/{pr_number}/timeline?per_page=100&page={page}"
        proc = run_gh(
            ["api", "-X", "GET", "-H", "Accept: application/vnd.github+json", path],
            env=config.gh_env,
        )
        data = json.loads(proc.stdout)
        if not isinstance(data, list):
            raise ValueError("gh timeline returned non-list JSON")
        items.extend(item for item in data if isinstance(item, dict))
        if len(data) < 100:
            break
    return items


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


def event_from_pr_timeline_item(
    raw_item: dict[str, Any],
    snapshot: PullRequestSnapshot,
    *,
    delivery_id: str,
    agent_login: str | None,
) -> ReviewEvent | None:
    event_type = str(raw_item.get("event") or "")
    if _timeline_item_is_issue_comment(raw_item):
        payload = _issue_comment_payload_from_timeline_item(
            raw_item, snapshot, action=_timeline_comment_action(raw_item)
        )
        return parse_github_event("issue_comment", delivery_id, payload, agent_login=agent_login)
    if event_type == "line-commented":
        payload = _review_comment_payload_from_timeline_item(
            raw_item,
            snapshot,
            action=_timeline_comment_action(raw_item),
        )
        if payload is None:
            return None
        return parse_github_event("pull_request_review_comment", delivery_id, payload, agent_login=agent_login)
    if event_type == "reviewed":
        payload = _review_payload_from_timeline_item(raw_item, snapshot, action=_timeline_review_action(raw_item))
        if payload is None:
            return None
        return parse_github_event("pull_request_review", delivery_id, payload, agent_login=agent_login)
    return None


def _repo_event_context(raw_event: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "events_poll",
        "repo_event_id": str(raw_event.get("id") or ""),
        "repo_event_type": str(raw_event.get("type") or ""),
        "repo_event_created_at": str(raw_event.get("created_at") or ""),
        "actor": str(raw_event.get("actor", {}).get("login") or "") if isinstance(raw_event.get("actor"), dict) else "",
    }


def _base_pr_payload(snapshot: PullRequestSnapshot, action: str) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": snapshot.repo},
        "pull_request": {
            "number": snapshot.number,
            "html_url": snapshot.url,
            "state": snapshot.state,
            "draft": snapshot.draft,
            "base": {"ref": snapshot.base_ref},
            "head": {
                "ref": snapshot.head_ref,
                "sha": snapshot.head_sha,
                "repo": {"full_name": snapshot.head_repo},
            },
            "updated_at": snapshot.updated_at,
        },
    }


def _issue_comment_payload_from_timeline_item(
    raw_item: dict[str, Any],
    snapshot: PullRequestSnapshot,
    *,
    action: str,
) -> dict[str, Any]:
    payload = {
        "action": action,
        "repository": {"full_name": snapshot.repo},
        "issue": {
            "number": snapshot.number,
            "html_url": snapshot.url,
            "state": snapshot.state,
            "pull_request": {"url": f"https://api.github.com/repos/{snapshot.repo}/pulls/{snapshot.number}"},
        },
        "comment": {
            "id": raw_item.get("id"),
            "node_id": raw_item.get("node_id"),
            "html_url": raw_item.get("html_url") or raw_item.get("url") or "",
            "body": raw_item.get("body") or "",
            "user": raw_item.get("user") if isinstance(raw_item.get("user"), dict) else {},
            "created_at": raw_item.get("created_at") or "",
            "updated_at": raw_item.get("updated_at") or raw_item.get("created_at") or "",
        },
    }
    payload["nyanpasu"] = _timeline_context(raw_item, source="pr_timeline_poll")
    return payload


def _review_comment_payload_from_timeline_item(
    raw_item: dict[str, Any],
    snapshot: PullRequestSnapshot,
    *,
    action: str,
) -> dict[str, Any] | None:
    comment = raw_item.get("comment")
    data = comment if isinstance(comment, dict) else raw_item
    if data.get("id") is None and data.get("node_id") is None:
        return None
    payload = _base_pr_payload(snapshot, action)
    payload["comment"] = {
        "id": data.get("id"),
        "node_id": data.get("node_id"),
        "html_url": data.get("html_url") or raw_item.get("html_url") or raw_item.get("url") or "",
        "body": data.get("body") or "",
        "user": data.get("user") if isinstance(data.get("user"), dict) else {},
        "path": data.get("path") or raw_item.get("path") or "",
        "line": data.get("line") or data.get("original_line") or raw_item.get("line") or raw_item.get("original_line"),
        "original_line": data.get("original_line") or raw_item.get("original_line"),
        "in_reply_to_id": data.get("in_reply_to_id") or raw_item.get("in_reply_to_id"),
        "pull_request_review_id": data.get("pull_request_review_id") or raw_item.get("pull_request_review_id"),
        "created_at": data.get("created_at") or raw_item.get("created_at") or "",
        "updated_at": data.get("updated_at") or raw_item.get("updated_at") or raw_item.get("created_at") or "",
    }
    payload["nyanpasu"] = _timeline_context(raw_item, source="pr_timeline_poll")
    return payload


def _review_payload_from_timeline_item(
    raw_item: dict[str, Any],
    snapshot: PullRequestSnapshot,
    *,
    action: str,
) -> dict[str, Any] | None:
    review = raw_item.get("review")
    data = review if isinstance(review, dict) else raw_item
    if data.get("id") is None and data.get("node_id") is None:
        return None
    payload = _base_pr_payload(snapshot, action)
    payload["review"] = {
        "id": data.get("id"),
        "node_id": data.get("node_id"),
        "html_url": data.get("html_url") or raw_item.get("html_url") or raw_item.get("url") or "",
        "body": data.get("body") or "",
        "state": data.get("state") or raw_item.get("state") or "",
        "user": data.get("user") if isinstance(data.get("user"), dict) else {},
        "submitted_at": data.get("submitted_at") or raw_item.get("submitted_at") or raw_item.get("created_at") or "",
    }
    payload["nyanpasu"] = _timeline_context(raw_item, source="pr_timeline_poll")
    return payload


def _timeline_context(raw_item: dict[str, Any], *, source: str) -> dict[str, Any]:
    return {
        "source": source,
        "timeline_item_id": _timeline_item_id(raw_item),
        "timeline_item_event": str(raw_item.get("event") or ""),
        "timeline_item_updated_at": _timeline_item_updated_at(raw_item),
    }


def _github_event_name(event_type: str) -> str | None:
    return {
        "IssueCommentEvent": "issue_comment",
        "PullRequestEvent": "pull_request",
        "PullRequestReviewCommentEvent": "pull_request_review_comment",
        "PullRequestReviewEvent": "pull_request_review",
        "PushEvent": "push",
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


def _delivery_id_from_timeline_item(repo: str, pr_number: int, raw_item: dict[str, Any]) -> str:
    item_id = _timeline_item_id(raw_item)
    if item_id:
        return f"timeline-poll-{safe_slug(repo)}-{pr_number}-{safe_slug(item_id)}"
    event = safe_slug(str(raw_item.get("event") or "item"))
    updated_at = safe_slug(_timeline_item_updated_at(raw_item))
    return f"timeline-poll-{safe_slug(repo)}-{pr_number}-{event}-{updated_at}"


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


def _sorted_timeline_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: (_timeline_item_updated_at(item), _timeline_item_id(item)))


def _timeline_items_after(
    items: list[dict[str, Any]],
    last_updated_at: str,
    cursor_ids: set[str],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for item in items:
        updated_at = _timeline_item_updated_at(item)
        if updated_at > last_updated_at:
            selected.append(item)
            continue
        if updated_at == last_updated_at:
            item_id = _timeline_item_id(item)
            if item_id not in cursor_ids:
                selected.append(item)
    return selected


def _timeline_items_strictly_after(items: list[dict[str, Any]], last_updated_at: str) -> list[dict[str, Any]]:
    return [item for item in items if _timeline_item_updated_at(item) > last_updated_at]


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


def _newest_timeline_cursor(
    repo: str,
    pr_number: int,
    items: list[dict[str, Any]],
) -> PullRequestTimelineCursor | None:
    if not items:
        return None
    newest_updated_at = max(_timeline_item_updated_at(item) for item in items)
    ids = tuple(
        _timeline_item_id(item)
        for item in items
        if _timeline_item_updated_at(item) == newest_updated_at and _timeline_item_id(item)
    )
    return PullRequestTimelineCursor(
        repo=repo,
        pr_number=pr_number,
        last_item_updated_at=newest_updated_at,
        item_ids=ids,
        initialized_at=time.time(),
        updated_at=time.time(),
    )


def _timeline_cursor_timestamp(cursor: PullRequestTimelineCursor, baseline: str) -> str:
    if cursor.last_item_updated_at < baseline:
        return baseline
    return cursor.last_item_updated_at


def _timeline_cursor_item_ids(
    cursor: PullRequestTimelineCursor,
    items: list[dict[str, Any]],
    baseline: str,
) -> tuple[str, ...]:
    timestamp = _timeline_cursor_timestamp(cursor, baseline)
    return tuple(_timeline_item_id(item) for item in items if _timeline_item_updated_at(item) == timestamp)


def _newest_pr_updated_cursor(repo: str, snapshots: list[PullRequestSnapshot]) -> PullRequestUpdatedCursor | None:
    if not snapshots:
        return None
    newest_updated_at = max(snapshot.updated_at for snapshot in snapshots)
    ids = tuple(snapshot.node_id for snapshot in snapshots if snapshot.updated_at == newest_updated_at)
    return PullRequestUpdatedCursor(
        repo=repo,
        last_updated_at=newest_updated_at,
        pr_node_ids=ids,
        initialized_at=time.time(),
        updated_at=time.time(),
    )


def _prs_after_updated_cursor(
    snapshots: list[PullRequestSnapshot],
    last_updated_at: str,
    cursor_ids: set[str],
) -> list[PullRequestSnapshot]:
    selected: list[PullRequestSnapshot] = []
    for snapshot in snapshots:
        if snapshot.updated_at > last_updated_at:
            selected.append(snapshot)
            continue
        if snapshot.updated_at == last_updated_at and snapshot.node_id not in cursor_ids:
            selected.append(snapshot)
    return selected


def _snapshot_from_pr(repo: str, raw: dict[str, Any]) -> PullRequestSnapshot:
    base = raw.get("base")
    head = raw.get("head")
    base_data = base if isinstance(base, dict) else {}
    head_data = head if isinstance(head, dict) else {}
    head_repo = head_data.get("repo")
    head_repo_data = head_repo if isinstance(head_repo, dict) else {}
    return PullRequestSnapshot(
        repo=repo,
        number=int(raw["number"]),
        node_id=str(raw.get("node_id") or raw.get("id") or raw["number"]),
        url=str(raw.get("html_url") or raw.get("url") or ""),
        state=str(raw.get("state") or "open").lower(),
        draft=bool(raw.get("draft", False)),
        base_ref=str(base_data.get("ref") or ""),
        head_ref=str(head_data.get("ref") or ""),
        head_repo=str(head_repo_data.get("full_name") or ""),
        head_sha=str(head_data.get("sha") or ""),
        title_hash=_hash_text(str(raw.get("title") or "")),
        body_hash=_hash_text(str(raw.get("body") or "")),
        created_at=_normalize_timestamp(str(raw.get("created_at") or "")),
        updated_at=_normalize_timestamp(str(raw.get("updated_at") or "")),
    )


def _event_from_snapshot_diff(
    snapshot: PullRequestSnapshot,
    previous: PullRequestSnapshot | None,
    *,
    agent_login: str | None,
    first_seen_after: str,
) -> ReviewEvent | None:
    if snapshot.draft:
        return None
    action = _snapshot_action(snapshot, previous, first_seen_after=first_seen_after)
    if action is None:
        return None
    payload = _payload_from_snapshot(snapshot, action=action)
    delivery_id = _synthetic_delivery_id(snapshot, action, previous)
    return parse_github_event("pull_request", delivery_id, payload, agent_login=agent_login)


def _snapshot_action(
    snapshot: PullRequestSnapshot,
    previous: PullRequestSnapshot | None,
    *,
    first_seen_after: str,
) -> str | None:
    if previous is None:
        return "opened" if snapshot.state == "open" and snapshot.created_at > first_seen_after else None
    if previous.state != "closed" and snapshot.state == "closed":
        return "closed"
    if previous.state == "closed" and snapshot.state == "open":
        return "reopened"
    if previous.draft and not snapshot.draft and snapshot.state == "open":
        return "ready_for_review"
    if previous.head_sha != snapshot.head_sha and snapshot.state == "open":
        return "synchronize"
    if (
        previous.title_hash != snapshot.title_hash
        or previous.body_hash != snapshot.body_hash
        or previous.base_ref != snapshot.base_ref
    ) and snapshot.state == "open":
        return "edited"
    return None


def _payload_from_snapshot(snapshot: PullRequestSnapshot, *, action: str) -> dict[str, Any]:
    return {
        "action": action,
        "repository": {"full_name": snapshot.repo},
        "pull_request": {
            "number": snapshot.number,
            "html_url": snapshot.url,
            "state": snapshot.state,
            "draft": snapshot.draft,
            "base": {"ref": snapshot.base_ref},
            "head": {
                "ref": snapshot.head_ref,
                "sha": snapshot.head_sha,
                "repo": {"full_name": snapshot.head_repo},
            },
            "updated_at": snapshot.updated_at,
        },
        "nyanpasu": {
            "source": "pr_state_poll",
            "trigger": f"pull_request_{action}",
            "trigger_summary": f"Pull request `{action}` event synthesized from PR state polling.",
        },
    }


def _synthetic_delivery_id(
    snapshot: PullRequestSnapshot,
    action: str,
    previous: PullRequestSnapshot | None,
) -> str:
    old = previous.head_sha if previous else "none"
    raw = f"{snapshot.repo}#{snapshot.number}:{action}:{old}->{snapshot.head_sha}:{snapshot.updated_at}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"synthetic-pr-state-{safe_slug(snapshot.repo)}-{snapshot.number}-{action}-{digest}"


def _journal_record_from_event(
    event: ReviewEvent,
    *,
    source: str,
    event_created_at: str,
) -> GitHubEventJournalRecord:
    if event.pr is None:
        repo = ""
        pr_number = None
    else:
        repo = event.pr.repo
        pr_number = event.pr.number
    now = time.time()
    return GitHubEventJournalRecord(
        delivery_id=event.delivery_id,
        dedupe_key=_dedupe_key(event),
        source=source,
        repo=repo,
        pr_number=pr_number,
        github_event=event.github_event,
        action=event.action,
        event_created_at=event_created_at,
        event=event,
        status=GitHubEventJournalStatus.PENDING,
        created_at=now,
        updated_at=now,
    )


def _log_poll_decision(
    *,
    source: str,
    decision: str,
    repo: str,
    reason: str | None = None,
    raw: dict[str, Any] | None = None,
    event: ReviewEvent | None = None,
    record: GitHubEventJournalRecord | None = None,
    delivery_id: str | None = None,
    pr_number: int | None = None,
    event_created_at: str | None = None,
    head_sha: str | None = None,
) -> None:
    event_created_at = event_created_at or record.event_created_at if record is not None else event_created_at
    logger.info(
        "poll event decision source={} decision={} reason={} repo={} pr={} delivery_id={} dedupe_key={} "
        "github_event={} trigger={} action={} event_created_at={} raw_type={} raw_id={} actor={} head_sha={}",
        source,
        decision,
        reason or "",
        _event_repo(repo, event, record),
        _event_pr_number(pr_number, event, record),
        delivery_id or (record.delivery_id if record is not None else event.delivery_id if event is not None else ""),
        record.dedupe_key if record is not None else "",
        record.github_event
        if record is not None
        else event.github_event
        if event is not None
        else _raw_event_name(raw),
        _event_trigger(event) if event is not None else "",
        (record.action.value if record is not None else event.action.value if event is not None else ""),
        event_created_at or _raw_updated_at(raw),
        _raw_event_name(raw),
        _raw_event_id(raw),
        _raw_actor(raw),
        head_sha or (event.after_sha if event is not None else ""),
    )


def _log_journal_decision(
    record: GitHubEventJournalRecord,
    decision: str,
    *,
    reason: str = "",
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    logger.info(
        "journal event decision decision={} reason={} repo={} pr={} delivery_id={} dedupe_key={} source={} "
        "github_event={} trigger={} action={} event_created_at={} result={} error={}",
        decision,
        reason,
        record.repo,
        record.pr_number or "",
        record.delivery_id,
        record.dedupe_key,
        record.source,
        record.github_event,
        _event_trigger(record.event),
        record.action.value,
        record.event_created_at,
        result or {},
        error or "",
    )


def _log_source_summary(
    *,
    source: str,
    repo: str,
    fetched: int,
    window: int,
    journaled: int,
    duplicates: int,
    ignored: int,
    baselined: int,
    ignored_reasons: Counter[str],
    pr_number: int | None = None,
) -> None:
    logger.info(
        "poll source summary source={} repo={} pr={} fetched={} window={} journaled={} duplicates={} ignored={} "
        "baselined={} ignored_reasons={}",
        source,
        repo,
        pr_number or "",
        fetched,
        window,
        journaled,
        duplicates,
        ignored,
        baselined,
        dict(ignored_reasons),
    )


def _event_repo(
    repo: str,
    event: ReviewEvent | None,
    record: GitHubEventJournalRecord | None,
) -> str:
    if record is not None and record.repo:
        return record.repo
    if event is not None and event.pr is not None:
        return event.pr.repo
    return repo


def _event_pr_number(
    pr_number: int | None,
    event: ReviewEvent | None,
    record: GitHubEventJournalRecord | None,
) -> str:
    if record is not None and record.pr_number is not None:
        return str(record.pr_number)
    if event is not None and event.pr is not None:
        return str(event.pr.number)
    return str(pr_number) if pr_number is not None else ""


def _repo_event_ignore_reason(raw_event: dict[str, Any], event: ReviewEvent | None) -> str:
    if event is None:
        if not isinstance(raw_event.get("payload"), dict):
            return "missing_payload"
        if _github_event_name(str(raw_event.get("type") or "")) is None:
            return "unsupported_repo_event_type"
        return "unparseable_repo_event"
    if event.action is ReviewAction.IGNORED:
        return _ignored_event_reason(event)
    return f"unsupported_action_{event.action.value}"


def _timeline_item_ignore_reason(raw_item: dict[str, Any], event: ReviewEvent | None) -> str:
    if event is None:
        event_type = str(raw_item.get("event") or "")
        if event_type not in {"commented", "line-commented", "reviewed"}:
            return "unsupported_timeline_item_type"
        if event_type in {"line-commented", "reviewed"}:
            return "missing_comment_or_review_id"
        return "unparseable_timeline_item"
    if event.action is ReviewAction.IGNORED:
        return _ignored_event_reason(event)
    return f"unsupported_action_{event.action.value}"


def _snapshot_ignore_reason(
    snapshot: PullRequestSnapshot,
    previous: PullRequestSnapshot | None,
    *,
    first_seen_after: str,
) -> str:
    if snapshot.draft:
        return "draft_pr"
    action = _snapshot_action(snapshot, previous, first_seen_after=first_seen_after)
    if action is None:
        if previous is None:
            return "old_pr_first_seen"
        return "no_relevant_snapshot_change"
    return f"ignored_snapshot_action_{action}"


def _ignored_event_reason(event: ReviewEvent) -> str:
    action = str(event.raw.get("action") or "action")
    if event.github_event == "pull_request":
        pr = event.pr
        if pr is not None and pr.draft:
            return "draft_pr"
        return f"ignored_pull_request_{action}"
    if event.github_event == "issue_comment":
        issue = event.raw.get("issue")
        if not isinstance(issue, dict) or not isinstance(issue.get("pull_request"), dict):
            return "not_pr_comment"
        if action not in {"created", "edited"}:
            return f"unsupported_issue_comment_{action}"
        return "comment_without_agent_mention"
    if event.github_event == "pull_request_review_comment":
        if action not in {"created", "edited", "updated"}:
            return f"unsupported_review_comment_{action}"
        if _event_is_from_nyanpasu(event):
            return "agent_authored_item"
        return "review_comment_without_reply_or_mention"
    if event.github_event == "pull_request_review":
        if action not in {"submitted", "edited", "created", "updated"}:
            return f"unsupported_review_{action}"
        if _event_is_from_nyanpasu(event):
            return "agent_authored_item"
        return "review_without_agent_mention"
    if event.github_event == "push":
        return "push_without_pull_request"
    return f"ignored_{event.github_event}"


def _event_is_from_nyanpasu(event: ReviewEvent) -> bool:
    context = event.raw.get("nyanpasu")
    return isinstance(context, dict) and context.get("source") == "pr_timeline_poll" and _event_trigger(event) == ""


def _raw_event_name(raw: dict[str, Any] | None) -> str:
    if raw is None:
        return ""
    return str(raw.get("type") or raw.get("event") or "")


def _raw_event_id(raw: dict[str, Any] | None) -> str:
    if raw is None:
        return ""
    return _event_id(raw) or _timeline_item_id(raw)


def _raw_actor(raw: dict[str, Any] | None) -> str:
    if raw is None:
        return ""
    actor = raw.get("actor")
    if isinstance(actor, dict) and actor.get("login"):
        return str(actor["login"])
    user = raw.get("user")
    if isinstance(user, dict) and user.get("login"):
        return str(user["login"])
    for key in ("comment", "review"):
        nested = raw.get(key)
        if not isinstance(nested, dict):
            continue
        user = nested.get("user")
        if isinstance(user, dict) and user.get("login"):
            return str(user["login"])
    return ""


def _raw_updated_at(raw: dict[str, Any] | None) -> str:
    if raw is None:
        return ""
    return _event_created_at(raw) or _timeline_item_updated_at(raw)


def _dedupe_key(event: ReviewEvent) -> str:
    context = event.raw.get("nyanpasu")
    if event.github_event == "pull_request" and event.pr is not None:
        trigger = str(context.get("trigger") or event.raw.get("action") or "") if isinstance(context, dict) else ""
        if trigger == "pull_request_synchronize" or event.raw.get("action") == "synchronize":
            return f"pull_request:synchronize:{event.pr.key}:{event.after_sha}"
        return f"pull_request:{event.raw.get('action')}:{event.pr.key}:{event.delivery_id}"
    if isinstance(context, dict):
        for field, prefix in (
            ("comment_id", event.github_event),
            ("pull_request_review_id", event.github_event),
            ("repo_event_id", event.github_event),
        ):
            value = context.get(field)
            if value:
                return f"{prefix}:{value}"
    return event.delivery_id


def _event_created_at(event: dict[str, Any]) -> str:
    value = str(event.get("created_at") or "")
    return _normalize_timestamp(value)


def _timeline_item_updated_at(item: dict[str, Any]) -> str:
    value = str(
        item.get("updated_at") or item.get("submitted_at") or item.get("created_at") or item.get("committed_at") or ""
    )
    return _normalize_timestamp(value)


def _timeline_item_id(item: dict[str, Any]) -> str:
    for key in ("node_id", "id"):
        value = item.get(key)
        if value:
            return str(value)
    for key in ("comment", "review"):
        nested = item.get(key)
        if not isinstance(nested, dict):
            continue
        for nested_key in ("node_id", "id"):
            value = nested.get(nested_key)
            if value:
                return str(value)
    sha = item.get("sha")
    if sha:
        return str(sha)
    digest_src = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(digest_src.encode("utf-8")).hexdigest()[:16]


def _timeline_item_is_issue_comment(item: dict[str, Any]) -> bool:
    if str(item.get("event") or "") != "commented":
        return False
    return "body" in item


def _timeline_comment_action(item: dict[str, Any]) -> str:
    updated_at = _normalize_timestamp(str(item.get("updated_at") or ""))
    created_at = _normalize_timestamp(str(item.get("created_at") or ""))
    return "edited" if updated_at and created_at and updated_at != created_at else "created"


def _timeline_review_action(item: dict[str, Any]) -> str:
    event = str(item.get("event") or "")
    if event == "reviewed":
        return "submitted"
    return _timeline_comment_action(item)


def _timeline_item_is_from_agent(item: dict[str, Any], agent_login: str | None) -> bool:
    if not agent_login:
        return False
    user = item.get("user")
    if not isinstance(user, dict):
        return False
    login = str(user.get("login") or "")
    return login.casefold() == agent_login.casefold()


def _normalize_timestamp(value: str) -> str:
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


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
