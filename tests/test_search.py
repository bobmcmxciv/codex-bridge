"""POST /v1/alpha/search: Codex standalone web search through the Codex backend.

Codex CLI 0.158 (feature StandaloneWebSearch) calls `{provider}/alpha/search`
itself instead of sending the hosted `web_search` tool; a provider without the
route answered 404 to every search. The bridge forwards to the same path under
CODEX_BASE with the pool's ChatGPT credentials.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_TMP = Path(tempfile.mkdtemp(prefix="codex-search-test-"))
os.environ.setdefault("CODEX_ACCOUNTS_CONFIG", str(_TMP / "pool.json"))
os.environ.setdefault("CODEX_ACCOUNTS_STATE", str(_TMP / "state.json"))
os.environ.setdefault("CODEX_CLIENT_VERSION", "9.9.9")

import fake_auth  # noqa: E402,F401
import codex_bridge  # noqa: E402


class _Creds:
    def __init__(self, account_id: str):
        self.account_id = account_id

    def get(self, force_refresh: bool = False):
        return f"token-{self.account_id}", self.account_id


class _Account:
    def __init__(self, account_id: str):
        self.id = account_id
        self.creds = _Creds(account_id)


class _Pool:
    def __init__(self, *ids: str):
        self.accounts_list = [_Account(i) for i in ids]
        self.events: list[tuple] = []

    def candidates(self):
        return list(self.accounts_list)

    def mark_ok(self, account):
        self.events.append(("ok", account.id))

    def mark_quota(self, account, resets_at=None, resets_in=None, detail=""):
        self.events.append(("quota", account.id))

    def mark_unavailable(self, account, seconds, reason, detail=""):
        self.events.append(("unavailable", account.id, reason))


class _Resp:
    def __init__(self, status: int, body=None, text: str | None = None):
        self.status_code = status
        self.text = text if text is not None else json.dumps(body or {})
        self.content = self.text.encode()
        self.headers = {"Content-Type": "application/json"}

    def close(self):
        pass


SEARCH_ANSWER = {
    "encrypted_output": "ciphertext",
    "output": "Search result",
    "results": [
        {"type": "text_result", "ref_id": "turn0search0", "url": "https://example.com/a",
         "future_field": {"preserved": True}},
    ],
}

QUOTA_429 = json.dumps({"error": {"type": "usage_limit_reached", "resets_in_seconds": 60}})


def _body(model: str = "gpt-6-sol") -> dict:
    return {
        "id": "search-session",
        "model": model,
        "input": [{"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": "Search the web"}]}],
        "commands": {"search_query": [{"q": "standalone web search"}]},
        "settings": {"allowed_callers": ["direct"]},
    }


@pytest.fixture()
def bridge(monkeypatch):
    monkeypatch.setattr(codex_bridge, "BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setattr(codex_bridge, "CODEX_MODEL", "gpt-6-sol")
    monkeypatch.setattr(codex_bridge, "_allowed_models", lambda: {"gpt-6-sol", "gpt-6-astra"})
    return codex_bridge


def _post_stub(monkeypatch, responses: list[_Resp]):
    calls: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return responses.pop(0)

    monkeypatch.setattr(codex_bridge.requests, "post", fake_post)
    return calls


AUTH = {"Authorization": "Bearer bridge-secret"}


def test_requires_bridge_token(bridge):
    response = bridge.app.test_client().post("/v1/alpha/search", json=_body())
    assert response.status_code == 401


def test_forwards_to_codex_alpha_search_and_returns_answer_verbatim(bridge, monkeypatch):
    pool = _Pool("pro")
    monkeypatch.setattr(bridge, "pool", pool)
    calls = _post_stub(monkeypatch, [_Resp(200, SEARCH_ANSWER)])

    response = bridge.app.test_client().post("/v1/alpha/search", json=_body(), headers=AUTH)

    assert response.status_code == 200
    assert response.json == SEARCH_ANSWER
    call = calls[0]
    assert call["url"] == f"{bridge.CODEX_BASE}/alpha/search"
    assert call["json"] == _body()
    assert call["headers"]["Authorization"] == "Bearer token-pro"
    assert call["headers"]["chatgpt-account-id"] == "pro"
    assert call["headers"]["originator"] == "codex_cli_rs"
    assert pool.events == [("ok", "pro")]


def test_unprefixed_route_is_the_same_endpoint(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    _post_stub(monkeypatch, [_Resp(200, SEARCH_ANSWER)])

    response = bridge.app.test_client().post("/alpha/search", json=_body(), headers=AUTH)

    assert response.status_code == 200
    assert response.json == SEARCH_ANSWER


def test_unknown_model_falls_back_to_codex_model(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    calls = _post_stub(monkeypatch, [_Resp(200, SEARCH_ANSWER)])

    bridge.app.test_client().post("/v1/alpha/search", json=_body("gpt-5.4"), headers=AUTH)

    assert calls[0]["json"]["model"] == "gpt-6-sol"


def test_allowed_model_is_kept(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    calls = _post_stub(monkeypatch, [_Resp(200, SEARCH_ANSWER)])

    bridge.app.test_client().post("/v1/alpha/search", json=_body("gpt-6-astra"), headers=AUTH)

    assert calls[0]["json"]["model"] == "gpt-6-astra"


def test_quota_429_fails_over_to_next_account(bridge, monkeypatch):
    pool = _Pool("pro", "lite")
    monkeypatch.setattr(bridge, "pool", pool)
    calls = _post_stub(monkeypatch, [_Resp(429, text=QUOTA_429), _Resp(200, SEARCH_ANSWER)])

    response = bridge.app.test_client().post("/v1/alpha/search", json=_body(), headers=AUTH)

    assert response.status_code == 200
    assert [c["headers"]["chatgpt-account-id"] for c in calls] == ["pro", "lite"]
    assert pool.events == [("quota", "pro"), ("ok", "lite")]


def test_upstream_error_is_returned_with_status(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    _post_stub(monkeypatch, [_Resp(400, text='{"detail": "bad commands"}')])

    response = bridge.app.test_client().post("/v1/alpha/search", json=_body(), headers=AUTH)

    assert response.status_code == 400
    assert "bad commands" in response.json["error"]["message"]


def test_non_json_upstream_answer_is_a_502(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    _post_stub(monkeypatch, [_Resp(200, text="<html>oops</html>")])

    response = bridge.app.test_client().post("/v1/alpha/search", json=_body(), headers=AUTH)

    assert response.status_code == 502
