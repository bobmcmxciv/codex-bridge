"""Hosted web search through the bridge.

Wire shapes captured against the ChatGPT Codex backend on 2026-09-28: it takes
a Responses `{"type": "web_search"}` tool (with `filters.allowed_domains`),
`tool_choice {"type": "web_search"}` and the `web_search_call.action.sources`
include, and streams `web_search_call` output items plus `url_citation`
annotations. The bridge relays those to chat/completions callers as a
`web_search_calls` extension and the standard `annotations` field.
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

# codex_bridge builds its pool at import time; point it somewhere disposable.
_TMP = Path(tempfile.mkdtemp(prefix="codex-websearch-test-"))
os.environ.setdefault("CODEX_ACCOUNTS_CONFIG", str(_TMP / "pool.json"))
os.environ.setdefault("CODEX_ACCOUNTS_STATE", str(_TMP / "state.json"))
os.environ.setdefault("CODEX_CLIENT_VERSION", "9.9.9")

import fake_auth  # noqa: E402,F401
import codex_bridge  # noqa: E402


def _chat_body(**extra) -> dict:
    body = {
        "model": "gpt-6-sol",
        "messages": [{"role": "user", "content": "Perform a web search for the query: x"}],
        "stream": True,
    }
    body.update(extra)
    return body


def _deltas(chunks) -> list[dict]:
    out = []
    for chunk in chunks:
        for line in chunk.split("\n"):
            if line.startswith("data: ") and line != "data: [DONE]":
                out.append(json.loads(line[6:]))
    return out


def test_web_search_tool_becomes_the_hosted_tool_with_sources(monkeypatch):
    monkeypatch.setattr(codex_bridge, "_allowed_models", lambda: {"gpt-6-sol"})
    body = _chat_body(
        tools=[
            {"type": "web_search", "filters": {"allowed_domains": ["openai.com"]}, "max_uses": 8},
            {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}},
        ],
        tool_choice={"type": "web_search"},
    )

    payload = codex_bridge.translate_request(body)

    assert payload["tools"][0] == {"type": "web_search", "filters": {"allowed_domains": ["openai.com"]}}
    assert payload["tools"][1]["type"] == "function" and payload["tools"][1]["name"] == "lookup"
    assert payload["tool_choice"] == {"type": "web_search"}
    assert "web_search_call.action.sources" in payload["include"]


def test_web_search_options_spelling_is_honoured(monkeypatch):
    monkeypatch.setattr(codex_bridge, "_allowed_models", lambda: {"gpt-6-sol"})
    body = _chat_body(web_search_options={
        "search_context_size": "low",
        "user_location": {"type": "approximate", "approximate": {"country": "CN", "city": "Nanjing"}},
    })

    payload = codex_bridge.translate_request(body)

    assert payload["tools"] == [{
        "type": "web_search",
        "search_context_size": "low",
        "user_location": {"type": "approximate", "country": "CN", "city": "Nanjing"},
    }]
    assert "web_search_call.action.sources" in payload["include"]


def test_forcing_web_search_without_the_tool_stays_auto(monkeypatch):
    monkeypatch.setattr(codex_bridge, "_allowed_models", lambda: {"gpt-6-sol"})
    body = _chat_body(tools=[], tool_choice={"type": "web_search"})

    payload = codex_bridge.translate_request(body)

    assert payload["tools"] == []
    assert payload["tool_choice"] == "auto"
    assert "web_search_call.action.sources" not in payload.get("include", [])


_CITATION = {
    "type": "url_citation",
    "url": "https://a.example/x?utm_source=openai",
    "title": "A",
    "start_index": 0,
    "end_index": 5,
}
_SEARCH_ITEM = {
    "id": "ws_1",
    "type": "web_search_call",
    "status": "completed",
    "action": {
        "type": "search",
        "query": "q1",
        "queries": ["q1"],
        "sources": [{"type": "url", "url": "https://a.example/x"}],
    },
}


def _events() -> list[dict]:
    return [
        {"type": "response.created"},
        {"type": "response.output_item.added",
         "item": {"id": "ws_1", "type": "web_search_call", "status": "in_progress"}},
        {"type": "response.output_item.done", "item": _SEARCH_ITEM},
        {"type": "response.output_text.delta", "delta": "Hello"},
        {"type": "response.output_text.annotation.added", "annotation": _CITATION},
        {"type": "response.output_item.done", "item": {
            "id": "msg_1", "type": "message",
            "content": [{"type": "output_text", "text": "Hello", "annotations": [_CITATION]}]}},
        {"type": "response.completed",
         "response": {"usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}}},
    ]


_ANNOTATION_OUT = {
    "type": "url_citation",
    "url_citation": {
        "url": "https://a.example/x?utm_source=openai",
        "title": "A",
        "start_index": 0,
        "end_index": 5,
    },
}
_CALL_OUT = {"id": "ws_1", "status": "completed", "action": _SEARCH_ITEM["action"]}


def test_stream_relays_search_calls_and_citations():
    chunks = list(codex_bridge.stream_translate(iter(_events()), "gpt-6-sol"))
    deltas = [c["choices"][0]["delta"] for c in _deltas(chunks)]

    assert {"web_search_calls": [_CALL_OUT]} in deltas
    assert {"annotations": [_ANNOTATION_OUT]} in deltas
    assert {"content": "Hello"} in deltas
    # The unfinished `.added` item carries no action and is not relayed.
    assert sum(1 for d in deltas if "web_search_calls" in d) == 1
    last = _deltas(chunks)[-1]
    assert last["choices"][0]["finish_reason"] == "stop"
    assert last["usage"]["prompt_tokens"] == 10
    assert chunks[-1] == "data: [DONE]\n\n"


def test_nonstream_carries_search_calls_and_citations():
    result = codex_bridge.collect_nonstream(iter(_events()), "gpt-6-sol")

    message = result["choices"][0]["message"]
    assert message["content"] == "Hello"
    assert message["web_search_calls"] == [_CALL_OUT]
    assert message["annotations"] == [_ANNOTATION_OUT]
    assert result["choices"][0]["finish_reason"] == "stop"


def test_plain_answers_carry_no_search_fields():
    events = [
        {"type": "response.output_text.delta", "delta": "Hi"},
        {"type": "response.completed", "response": {"usage": {}}},
    ]

    message = codex_bridge.collect_nonstream(iter(events), "gpt-6-sol")["choices"][0]["message"]
    deltas = [c["choices"][0]["delta"] for c in _deltas(codex_bridge.stream_translate(iter(events), "gpt-6-sol"))]

    assert "web_search_calls" not in message and "annotations" not in message
    assert not any("web_search_calls" in d or "annotations" in d for d in deltas)
