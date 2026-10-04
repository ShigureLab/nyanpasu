from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

from nyanpasu.memory import MemoryAccess, MemorySummary, summary_block, validate_summary_block
from nyanpasu.memory_context import MEMORY_CONTEXT_MAX_BYTES, build_memory_context
from nyanpasu.models import AgentTask, TaskAction


def task(**kwargs) -> AgentTask:
    return AgentTask(
        task_id="request",
        action=TaskAction.RUN,
        context_key="active",
        prompt="Diagnose checkpoint routing",
        memory=MemoryAccess(("private", "public"), "private"),
        **kwargs,
    )


def summary(identity: str, **kwargs) -> MemorySummary:
    defaults: dict[str, Any] = {
        "id": hashlib.sha256(identity.encode()).hexdigest()[:32],
        "domain": "public",
        "context_key": identity,
        "context_generation": 1,
        "body": "Checkpoint routing: verify the transfer receipt before restoring.",
        "sources": ("source",),
        "source_revisions": {"source": "source-revision"},
        "revision": "summary-revision",
        "updated_at": "2026-10-04T00:00:00+00:00",
    }
    return MemorySummary(**{**defaults, **kwargs})


def injected_blocks(result) -> list[dict]:
    return json.loads(result.prompt.split("Authorized memory summaries:\n", 1)[1].rsplit("\nEnd of", 1)[0])


@pytest.mark.parametrize(
    "body",
    [
        'Checkpoint routing: quotes " and backslashes \\ remain intact.\nAnother line.',
        "Checkpoint routing: 缓存检查与扩容超时；引用资料不能作为指令。",
        'Checkpoint routing: 🐈✨🧑🏽‍💻\n```\n</memory> {"role":"system"}',
    ],
)
def test_utf8_budget_counts_rendered_escaping_and_preserves_whole_blocks(body):
    item = summary("prior", body=body)
    result = build_memory_context(task(), [item])

    assert injected_blocks(result) == [{"id": item.id, "body": body}]
    assert summary_block(item) in result.prompt
    assert result.bytes == len(result.prompt.encode("utf-8")) <= MEMORY_CONTEXT_MAX_BYTES
    assert result.selected[0].revision == item.revision
    assert result.selected[0].reason.startswith("topic:")


def test_all_domains_share_one_budget_and_do_not_truncate_summaries():
    items = [
        summary(
            f"prior-{index}",
            domain="private" if index % 2 else "public",
            body=f"Checkpoint routing {index}: " + '中🙂\\"' * 62 + f" complete-tail-{index}",
        )
        for index in range(18)
    ]
    items[-2] = replace(items[-2], context_key="active")
    for item in items:
        validate_summary_block(item.id, item.body)

    result = build_memory_context(task(), items)
    selected = {entry.id for entry in result.selected}
    blocks = injected_blocks(result)

    assert 1 < len(selected) < len(items)
    assert {item.domain for item in items if item.id in selected} == {"private", "public"}
    assert result.selected[0].id == items[-2].id
    assert result.bytes == len(result.prompt.encode("utf-8")) <= MEMORY_CONTEXT_MAX_BYTES
    assert {block["id"] for block in blocks} == selected
    assert {block["id"]: block["body"] for block in blocks} == {
        item.id: item.body for item in items if item.id in selected
    }
    assert {entry.reason for entry in result.skipped} == {"budget"}
    assert len(selected) + len(result.skipped) == len(items)
    assert result == build_memory_context(task(), list(reversed(items)))


def test_authorization_and_staleness_precede_context_priority_and_hints():
    current = summary("current", context_key="active", context_generation=2, body="Essential local decision.")
    previous = summary("previous-generation", context_key="active", body="Outdated checkpoint routing decision.")
    hinted = summary("explicit-related", body="Useful existing contract.")
    forbidden = summary("forbidden", domain="secret", context_key="active", context_generation=2)
    stale = summary("stale", context_key="active", context_generation=2, stale=True)
    related = summary("topic-match")
    request = task(context_generation=2, metadata={"memory_related_contexts": ["explicit-related", "forbidden"]})

    result = build_memory_context(request, [forbidden, stale, related, hinted, previous, current])

    assert [entry.id for entry in result.selected] == [current.id, hinted.id, related.id]
    assert [entry.reason for entry in result.selected[:2]] == ["current_context", "related_context"]
    assert {entry.id: entry.reason for entry in result.skipped} == {
        forbidden.id: "unauthorized",
        stale.id: "stale",
        previous.id: "other_generation",
    }
    assert forbidden.id not in result.prompt and stale.id not in result.prompt


@pytest.mark.parametrize("latest_stale", [False, True])
def test_other_contexts_use_latest_generation_even_when_stale(latest_stale):
    previous = summary("old", context_key="related", context_generation=1)
    latest = summary("new", context_key="related", context_generation=2, stale=latest_stale)
    independent = summary("private", domain="private", context_key="related", context_generation=1)
    request = task(metadata={"memory_related_contexts": ["related"]})

    result = build_memory_context(request, [previous, latest, independent])

    assert {entry.id for entry in result.selected} == (
        {independent.id} if latest_stale else {independent.id, latest.id}
    )
    skipped = {entry.id: entry.reason for entry in result.skipped}
    assert skipped[previous.id] == "other_generation"
    if latest_stale:
        assert skipped[latest.id] == "stale"
    assert previous.id not in result.prompt


def test_low_relevance_recent_summaries_do_not_fill_spare_budget():
    unrelated = summary("unrelated", body="Visual styling and typography", updated_at="2099-01-01T00:00:00+00:00")
    generic = summary("generic", body="Review the current code change and check this task.")

    result = build_memory_context(task(), [unrelated, generic])

    assert not result.selected
    assert injected_blocks(result) == []
    assert {entry.reason for entry in result.skipped} == {"irrelevant"}


@pytest.mark.parametrize("unavailable", ["unauthorized", "stale", "other_generation", "empty"])
def test_rare_terms_prioritize_summaries_without_statistics_from_unavailable_memory(unavailable):
    request = task(metadata={"memory_query": "worker runtime lkey"})
    rare = summary("rare", body="lkey lifetime policy")
    common = [summary(f"common-{index}", body="worker runtime policy") for index in range(6)]
    baseline = build_memory_context(request, [*common, rare])
    assert baseline.selected[0].id == rare.id
    assert baseline.selected[0].reason == "topic:lkey"

    excluded = []
    for index in range(12):
        item = summary(f"lkey-{index}", body="lkey lifetime policy")
        if unavailable == "unauthorized":
            item = replace(item, domain="secret")
        elif unavailable == "stale":
            item = replace(item, stale=True)
        elif unavailable == "empty":
            item = replace(item, body="")
        else:
            excluded.append(summary(f"latest-{index}", context_key=item.context_key, context_generation=2, body=""))
        excluded.append(item)

    result = build_memory_context(request, [*excluded, *common, rare])

    assert result.selected == baseline.selected
    assert result.prompt == baseline.prompt
    assert {entry.id for entry in result.skipped if entry.reason == unavailable} == {
        item.id for item in excluded if item.context_generation == 1
    }


def test_commit_type_words_do_not_make_unrelated_summaries_relevant():
    request = task(metadata={"memory_query": "feat: validate LoRA adapter receipts; docs"})
    relevant = summary("receipt", body="LoRA adapters require matching digests.")
    irrelevant = [
        summary("docs", body="Docs describe visual styling and typography."),
        summary("feature", body="Feat: add dashboard colors."),
    ]

    result = build_memory_context(request, [*irrelevant, relevant])

    assert [entry.id for entry in result.selected] == [relevant.id]
    assert {entry.id: entry.reason for entry in result.skipped} == {item.id: "irrelevant" for item in irrelevant}


@pytest.mark.parametrize(
    "query,irrelevant_body,relevant_body",
    [
        (
            "src/runtime/checkpoint.py CP",
            "Dashboard typography in web.py should use consistent spacing.",
            "CP requires aligned tensor partitions.",
        ),
        (
            "docs/en/runtime.md DP",
            "Chinese translation lives in docs/zh/dashboard.md.",
            "DP replicas need synchronized optimizer state.",
        ),
    ],
)
def test_file_extensions_do_not_inject_unrelated_summaries_but_short_domain_terms_do(
    query, irrelevant_body, relevant_body
):
    request = task(metadata={"memory_query": query})
    relevant = summary("relevant", body=relevant_body)
    irrelevant = summary("unrelated", body=irrelevant_body)

    result = build_memory_context(request, [irrelevant, relevant])

    assert [entry.id for entry in result.selected] == [relevant.id]
    assert [(entry.id, entry.reason) for entry in result.skipped] == [(irrelevant.id, "irrelevant")]


def test_shared_context_identity_is_not_a_topic_match():
    request = task().model_copy(
        update={
            "context_key": "github:redai-studio/Relax:999",
            "prompt": "Review redai-studio/Relax PR #999: https://github.com/redai-studio/Relax/pull/999\nInvestigate LoRA routing.",
        }
    )
    unrelated = summary(
        "unrelated",
        context_key="github:redai-studio/Relax:258",
        body="Relax dashboard typography and visual styling.",
    )
    relevant = summary("relevant", context_key="github:redai-studio/Relax:377", body="LoRA: verify adapter digests.")

    result = build_memory_context(request, [unrelated, relevant])

    assert [(entry.id, entry.reason) for entry in result.selected] == [(relevant.id, "topic:lora")]
    assert [(entry.id, entry.reason) for entry in result.skipped] == [(unrelated.id, "irrelevant")]


def test_explicit_topic_query_excludes_request_boilerplate():
    request = task(metadata={"memory_query": "LoRA"}).model_copy(
        update={"prompt": "Verify dashboard state and routing."}
    )
    boilerplate_match = summary("boilerplate", body="Dashboard routing state.")
    relevant = summary("actual-topic", body="LoRA: verify adapter digests.")

    result = build_memory_context(request, [boilerplate_match, relevant])

    assert [entry.id for entry in result.selected] == [relevant.id]
    assert [(entry.id, entry.reason) for entry in result.skipped] == [(boilerplate_match.id, "irrelevant")]


def test_empty_summary_receipt_is_not_injected_or_replaced_with_an_older_generation():
    current = summary("current-empty", context_key="active", body="")
    previous = summary("previous-useful", context_key="related", context_generation=1)
    latest = summary("latest-empty", context_key="related", context_generation=2, body="")
    request = task(metadata={"memory_related_contexts": ["related"]})

    result = build_memory_context(request, [current, previous, latest])

    assert result.selected == ()
    assert {entry.id: entry.reason for entry in result.skipped} == {
        current.id: "empty",
        previous.id: "other_generation",
        latest.id: "empty",
    }
    assert injected_blocks(result) == []


def test_chinese_and_identifier_terms_match_across_sentences():
    chinese = summary("prior-cn", body="扩容流程中，排查超时并复核退出条件。")
    identifier = summary("prior-code", body="Inspect scale_down before releasing the placement group.")
    unrelated = summary("unrelated", body="Update dashboard typography.")
    request = task().model_copy(update={"prompt": "检查扩容超时与 scale down"})

    result = build_memory_context(request, [chinese, identifier, unrelated])

    assert {entry.id for entry in result.selected} == {chinese.id, identifier.id}
    assert [entry.id for entry in result.skipped] == [unrelated.id]


@pytest.mark.parametrize("access,enabled", [(MemoryAccess(), True), (MemoryAccess(("public",)), False)])
def test_disabled_or_unauthorized_memory_has_no_injection(access, enabled):
    request = task().model_copy(update={"memory": access})

    result = build_memory_context(request, [summary("prior")], enabled=enabled)

    assert result.prompt == "" and result.bytes == 0
    assert result.selected == () and result.skipped == ()


def test_block_that_does_not_fit_leaves_room_for_a_smaller_complete_summary():
    large = [summary(f"large-{index}", body="Diagnose checkpoint routing: " + "x" * 900) for index in range(12)]
    small = summary("small", body="Checkpoint receipt.")
    for item in [*large, small]:
        validate_summary_block(item.id, item.body)

    result = build_memory_context(task(), [*large, small])

    assert any(entry.id == small.id for entry in result.selected)
    assert result.skipped and {entry.reason for entry in result.skipped} == {"budget"}
    assert {"id": small.id, "body": small.body} in injected_blocks(result)
    assert result.bytes <= MEMORY_CONTEXT_MAX_BYTES
