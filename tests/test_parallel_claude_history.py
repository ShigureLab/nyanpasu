from __future__ import annotations

from nyanpasu.transcript.claude import claude_history


def record(identity, parent, role, blocks, **extra):
    return {"uuid": identity, "parentUuid": parent, "type": role, "message": {"content": blocks}, **extra}


def call(identity):
    return {"type": "tool_use", "id": identity, "name": "Bash", "input": {"command": "true"}}


def result(identity, text):
    return {"type": "tool_result", "tool_use_id": identity, "content": text}


def test_parallel_sibling_results_are_attached_only_to_current_branch_calls():
    records = [
        record("user", None, "user", [{"type": "text", "text": "check both"}]),
        record("first", "user", "assistant", [call("tool-1")]),
        record("second", "first", "assistant", [call("tool-2")]),
        record("first-result", "first", "user", [result("tool-1", "first verified result")]),
        record("second-result", "second", "user", [result("tool-2", "second verified result")]),
        record("other-branch", "user", "assistant", [call("tool-3")]),
        record("other-result", "other-branch", "user", [result("tool-3", "unrelated branch")]),
        record("spoof", "other-branch", "user", [result("tool-1", "wrong parent")]),
        record("side", "first", "user", [result("tool-1", "sidechain")], isSidechain=True),
        record("final", "second-result", "assistant", [{"type": "text", "text": "done"}]),
    ]
    history = claude_history("session", records)
    tools = {item.id: item for turn in history.turns for item in turn.items if item.presentation.kind == "tool"}
    assert set(tools) == {"tool-1", "tool-2"}
    assert all(item.presentation.state == "completed" for item in tools.values())
    assert tools["tool-1"].presentation.blocks[-1].text == "first verified result"
    assert tools["tool-2"].presentation.blocks[-1].text == "second verified result"
