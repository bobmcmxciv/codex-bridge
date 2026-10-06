"""System messages between turns stay in place instead of joining `instructions`.

Folding every system message into `instructions` rewrote the start of the
prompt whenever a client added one mid-conversation (Claude Code does so on
most turns), which capped the upstream prompt cache at the part before it.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TMP = Path(tempfile.mkdtemp(prefix="codex-sysmsg-test-"))
os.environ.setdefault("CODEX_ACCOUNTS_CONFIG", str(_TMP / "pool.json"))
os.environ.setdefault("CODEX_ACCOUNTS_STATE", str(_TMP / "state.json"))
os.environ.setdefault("CODEX_CLIENT_VERSION", "9.9.9")

import fake_auth  # noqa: E402,F401
import codex_bridge  # noqa: E402


def _body(messages):
    return {"model": codex_bridge.CODEX_MODEL, "messages": messages}


def test_leading_system_messages_become_instructions():
    payload = codex_bridge.translate_request(_body([
        {"role": "system", "content": "base prompt"},
        {"role": "system", "content": "more rules"},
        {"role": "user", "content": "hi"},
    ]))
    assert payload["instructions"] == "base prompt\n\nmore rules"
    assert [i["role"] for i in payload["input"]] == ["user"]


def test_mid_conversation_system_message_stays_in_place_as_developer():
    payload = codex_bridge.translate_request(_body([
        {"role": "system", "content": "base prompt"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "hook context"},
        {"role": "assistant", "content": "hello"},
    ]))
    assert payload["instructions"] == "base prompt"
    assert payload["input"][1] == {
        "type": "message", "role": "developer",
        "content": [{"type": "input_text", "text": "hook context"}],
    }
    assert [i.get("role") for i in payload["input"]] == ["user", "developer", "assistant"]


def test_new_system_message_leaves_earlier_prompt_untouched():
    history = [
        {"role": "system", "content": "base prompt"},
        {"role": "user", "content": "go"},
        {"role": "system", "content": "<total_tokens>1</total_tokens>"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "a b c"},
    ]
    before = codex_bridge.translate_request(_body(history))
    after = codex_bridge.translate_request(_body(history + [
        {"role": "system", "content": "<total_tokens>2</total_tokens>"},
        {"role": "user", "content": "next"},
    ]))
    assert after["instructions"] == before["instructions"]
    assert after["input"][: len(before["input"])] == before["input"]
