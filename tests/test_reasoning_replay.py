"""A turn's reasoning items reach the caller and come back on the next request.

With store=false nothing survives between requests upstream; without the
replay every step of a tool loop started without the reasoning behind it.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TMP = Path(tempfile.mkdtemp(prefix="codex-reasoning-test-"))
os.environ.setdefault("CODEX_ACCOUNTS_CONFIG", str(_TMP / "pool.json"))
os.environ.setdefault("CODEX_ACCOUNTS_STATE", str(_TMP / "state.json"))
os.environ.setdefault("CODEX_CLIENT_VERSION", "9.9.9")

import fake_auth  # noqa: E402,F401
import codex_bridge  # noqa: E402

RS = {"type": "reasoning", "id": "rs_1", "encrypted_content": "gAAAA-enc", "summary": [], "content": []}
FC = {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "Read", "arguments": "{}"}


def _events():
    return [
        {"type": "response.output_item.added", "item": {"type": "reasoning", "id": "rs_1"}},
        {"type": "response.output_item.done", "item": RS},
        {"type": "response.output_item.added", "item": FC},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_1", "delta": "{}"},
        {"type": "response.output_item.done", "item": FC},
        {"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 2}}},
    ]


def _deltas(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: {"):
            out.append(json.loads(c[6:])["choices"][0]["delta"])
    return out


def test_request_asks_for_encrypted_reasoning():
    payload = codex_bridge.translate_request({"messages": [{"role": "user", "content": "hi"}]})
    assert payload["include"] == ["reasoning.encrypted_content"]


def test_web_search_include_is_kept_alongside():
    payload = codex_bridge.translate_request({
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "web_search"}],
    })
    assert payload["include"] == ["reasoning.encrypted_content", "web_search_call.action.sources"]


def test_stream_carries_reasoning_items_before_the_tool_call():
    deltas = _deltas(codex_bridge.stream_translate(iter(_events()), "gpt-6-luna"))
    kinds = [next((k for k in ("reasoning_items", "tool_calls") if k in d), None) for d in deltas]
    assert kinds.index("reasoning_items") < kinds.index("tool_calls")
    item = next(d["reasoning_items"] for d in deltas if "reasoning_items" in d)
    assert item == [{"encrypted_content": "gAAAA-enc", "summary": []}]


def test_nonstream_message_carries_reasoning_items():
    msg = codex_bridge.collect_nonstream(iter(_events()), "gpt-6-luna")["choices"][0]["message"]
    assert msg["reasoning_items"] == [{"encrypted_content": "gAAAA-enc", "summary": []}]


def test_reasoning_items_are_replayed_before_the_assistant_output():
    payload = codex_bridge.translate_request({"messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": None,
         "reasoning_items": [{"encrypted_content": "gAAAA-enc", "summary": []}],
         "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
    ]})
    types = [i["type"] if i["type"] != "message" else i["role"] for i in payload["input"]]
    assert types == ["user", "reasoning", "function_call", "function_call_output"]
    assert payload["input"][1] == {"type": "reasoning", "encrypted_content": "gAAAA-enc", "summary": []}


def test_reasoning_without_output_is_not_replayed():
    payload = codex_bridge.translate_request({"messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "reasoning_items": [{"encrypted_content": "x"}]},
        {"role": "user", "content": "again"},
    ]})
    assert all(i["type"] != "reasoning" for i in payload["input"])
