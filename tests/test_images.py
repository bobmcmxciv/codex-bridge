"""POST /v1/images/generations: gpt-image-2 through the Codex backend.

Wire format captured from Codex CLI 0.153.4 on 2026-09-06: the CLI's built-in
image tool is a client-side `POST {CODEX_BASE}/images/generations` carrying the
same ChatGPT bearer as /responses; the answer is OpenAI Images shaped. The
bridge walks the account pool like call_upstream() and serves `n` by
repeating the call.
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

# codex_bridge builds its pool at import time; point it somewhere disposable.
_TMP = Path(tempfile.mkdtemp(prefix="codex-images-test-"))
os.environ.setdefault("CODEX_ACCOUNTS_CONFIG", str(_TMP / "pool.json"))
os.environ.setdefault("CODEX_ACCOUNTS_STATE", str(_TMP / "state.json"))
# Pin the version so importing the module never shells out to a real CLI.
os.environ.setdefault("CODEX_CLIENT_VERSION", "9.9.9")

import fake_auth  # noqa: E402,F401
import codex_bridge  # noqa: E402


class _Creds:
    def __init__(self, account_id: str):
        self.account_id = account_id
        self.refreshes = 0

    def get(self, force_refresh: bool = False):
        if force_refresh:
            self.refreshes += 1
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

    def active(self):
        return self.accounts_list[0]

    def mark_ok(self, account):
        self.events.append(("ok", account.id))

    def mark_quota(self, account, resets_at=None, resets_in=None, detail=""):
        self.events.append(("quota", account.id))

    def mark_unavailable(self, account, seconds, reason, detail=""):
        self.events.append(("unavailable", account.id, reason))


class _Resp:
    def __init__(self, status: int, body=None, text: str | None = None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else json.dumps(body or {})
        self.closed = False

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    def close(self):
        self.closed = True


def _image(image_tokens: int = 229, input_tokens: int = 25) -> dict:
    return {
        "created": 1788702470,
        "background": "opaque",
        "data": [{"b64_json": "QUJD"}],
        "output_format": "png",
        "quality": "low",
        "size": "1254x1254",
        "usage": {
            "input_tokens": input_tokens,
            "input_tokens_details": {"image_tokens": 0, "text_tokens": input_tokens},
            "output_tokens": image_tokens,
            "output_tokens_details": {"image_tokens": image_tokens, "text_tokens": 0},
            "total_tokens": input_tokens + image_tokens,
        },
    }


@pytest.fixture()
def bridge(monkeypatch):
    monkeypatch.setattr(codex_bridge, "BRIDGE_TOKEN", "bridge-secret")
    monkeypatch.setattr(codex_bridge, "IMAGE_MODEL", "gpt-image-2")
    return codex_bridge


def _post_stub(monkeypatch, responses: list[_Resp]):
    calls: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return responses.pop(0)

    monkeypatch.setattr(codex_bridge.requests, "post", fake_post)
    return calls


def _client(bridge):
    return bridge.app.test_client()


def test_requires_bridge_token(bridge):
    response = _client(bridge).post("/v1/images/generations", json={"prompt": "a cat"})
    assert response.status_code == 401


def test_prompt_is_required(bridge):
    response = _client(bridge).post(
        "/v1/images/generations", json={"prompt": "  "},
        headers={"Authorization": "Bearer bridge-secret"},
    )
    assert response.status_code == 400
    assert "prompt" in response.json["error"]["message"]


def test_n_is_bounded(bridge):
    response = _client(bridge).post(
        "/v1/images/generations", json={"prompt": "x", "n": 5},
        headers={"Authorization": "Bearer bridge-secret"},
    )
    assert response.status_code == 400


def test_single_image_uses_codex_headers_and_defaults(bridge, monkeypatch):
    pool = _Pool("pro")
    monkeypatch.setattr(bridge, "pool", pool)
    calls = _post_stub(monkeypatch, [_Resp(200, _image())])

    response = _client(bridge).post(
        "/v1/images/generations", json={"prompt": "a purple star"},
        headers={"x-api-key": "bridge-secret"},
    )

    assert response.status_code == 200
    body = response.json
    assert body["data"] == [{"b64_json": "QUJD"}]
    assert body["model"] == "gpt-image-2"
    assert body["size"] == "1254x1254"
    assert body["usage"]["output_tokens_details"]["image_tokens"] == 229

    call = calls[0]
    assert call["url"] == f"{bridge.CODEX_BASE}/images/generations"
    # Same shape the Codex CLI sends: auto everything, model pinned server-side.
    assert call["json"] == {
        "prompt": "a purple star", "model": "gpt-image-2",
        "size": "auto", "quality": "auto", "background": "auto",
    }
    h = call["headers"]
    assert h["Authorization"] == "Bearer token-pro"
    assert h["chatgpt-account-id"] == "pro"
    assert h["originator"] == "codex_cli_rs"
    assert h["version"] == bridge.CODEX_CLIENT_VERSION
    assert pool.events == [("ok", "pro")]


def test_explicit_size_quality_are_forwarded(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    calls = _post_stub(monkeypatch, [_Resp(200, _image())])

    _client(bridge).post(
        "/v1/images/generations",
        json={"prompt": "x", "size": "1536x1024", "quality": "high", "background": "opaque"},
        headers={"x-api-key": "bridge-secret"},
    )

    sent = calls[0]["json"]
    assert (sent["size"], sent["quality"], sent["background"]) == ("1536x1024", "high", "opaque")


def test_n_repeats_the_call_and_merges_data_and_usage(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    calls = _post_stub(monkeypatch, [_Resp(200, _image(229, 25)), _Resp(200, _image(301, 25))])

    response = _client(bridge).post(
        "/v1/images/generations", json={"prompt": "x", "n": 2},
        headers={"x-api-key": "bridge-secret"},
    )

    assert response.status_code == 200
    assert len(calls) == 2
    assert "n" not in calls[0]["json"]
    body = response.json
    assert len(body["data"]) == 2
    assert body["usage"]["input_tokens"] == 50
    assert body["usage"]["output_tokens_details"]["image_tokens"] == 530
    assert body["usage"]["total_tokens"] == 580


def test_401_forces_one_token_refresh(bridge, monkeypatch):
    pool = _Pool("pro")
    monkeypatch.setattr(bridge, "pool", pool)
    calls = _post_stub(monkeypatch, [_Resp(401, text="expired"), _Resp(200, _image())])

    response = _client(bridge).post(
        "/v1/images/generations", json={"prompt": "x"},
        headers={"x-api-key": "bridge-secret"},
    )

    assert response.status_code == 200
    assert len(calls) == 2
    assert pool.accounts_list[0].creds.refreshes == 1
    assert pool.events == [("ok", "pro")]


def test_429_parks_the_account_and_fails_over(bridge, monkeypatch):
    pool = _Pool("pro", "spare")
    monkeypatch.setattr(bridge, "pool", pool)
    calls = _post_stub(monkeypatch, [_Resp(429, text="usage_limit_reached"), _Resp(200, _image())])

    response = _client(bridge).post(
        "/v1/images/generations", json={"prompt": "x"},
        headers={"x-api-key": "bridge-secret"},
    )

    assert response.status_code == 200
    assert calls[0]["headers"]["Authorization"] == "Bearer token-pro"
    assert calls[1]["headers"]["Authorization"] == "Bearer token-spare"
    assert pool.events[0][1] == "pro" and pool.events[0][0] in ("quota", "unavailable")
    assert pool.events[-1] == ("ok", "spare")


def test_upstream_400_is_returned_as_is(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    _post_stub(monkeypatch, [_Resp(400, {"detail": "Unsupported parameter: size"})])

    response = _client(bridge).post(
        "/v1/images/generations", json={"prompt": "x", "size": "7x7"},
        headers={"x-api-key": "bridge-secret"},
    )

    assert response.status_code == 400
    assert response.json["error"]["type"] == "bridge_error"
    assert "Unsupported parameter" in response.json["error"]["message"]


def test_all_accounts_exhausted_reports_last_failure(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro", "spare"))
    _post_stub(monkeypatch, [_Resp(429, text="usage_limit_reached"), _Resp(429, text="usage_limit_reached")])

    response = _client(bridge).post(
        "/v1/images/generations", json={"prompt": "x"},
        headers={"x-api-key": "bridge-secret"},
    )

    assert response.status_code == 429


# --- /v1/images/edits -----------------------------------------------------


def _edit(image_tokens_in: int = 1521) -> dict:
    piece = _image(229, 60)
    piece["usage"]["input_tokens"] = 60 + image_tokens_in
    piece["usage"]["input_tokens_details"] = {"image_tokens": image_tokens_in, "text_tokens": 60}
    piece["usage"]["total_tokens"] = 60 + image_tokens_in + 229
    return piece


def test_edits_requires_images(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    response = _client(bridge).post(
        "/v1/images/edits", json={"prompt": "x"},
        headers={"x-api-key": "bridge-secret"},
    )
    assert response.status_code == 400
    assert "images" in response.json["error"]["message"]


def test_edits_normalizes_inputs_and_posts_to_edits(bridge, monkeypatch):
    pool = _Pool("pro")
    monkeypatch.setattr(bridge, "pool", pool)
    calls = _post_stub(monkeypatch, [_Resp(200, _edit())])

    response = _client(bridge).post(
        "/v1/images/edits",
        json={
            "prompt": "place the animal from image 1 in a forest, same identity",
            "images": [
                {"image_url": "data:image/png;base64,AAAA"},   # dict form, kept
                "data:image/jpeg;base64,BBBB",                  # data URL string, kept
                "CCCC",                                          # bare base64 -> png data URL
            ],
            "size": "1024x1536",
        },
        headers={"x-api-key": "bridge-secret"},
    )

    assert response.status_code == 200
    call = calls[0]
    assert call["url"] == f"{bridge.CODEX_BASE}/images/edits"
    assert call["json"]["images"] == [
        {"image_url": "data:image/png;base64,AAAA"},
        {"image_url": "data:image/jpeg;base64,BBBB"},
        {"image_url": "data:image/png;base64,CCCC"},
    ]
    assert call["json"]["size"] == "1024x1536"
    assert call["json"]["model"] == "gpt-image-2"
    assert "n" not in call["json"]
    assert response.json["usage"]["input_tokens_details"]["image_tokens"] == 1521
    assert pool.events == [("ok", "pro")]


def test_edits_accepts_single_image_field(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    calls = _post_stub(monkeypatch, [_Resp(200, _edit())])

    response = _client(bridge).post(
        "/v1/images/edits", json={"prompt": "x", "image": "data:image/png;base64,AAAA"},
        headers={"x-api-key": "bridge-secret"},
    )

    assert response.status_code == 200
    assert calls[0]["json"]["images"] == [{"image_url": "data:image/png;base64,AAAA"}]


def test_edits_rejects_too_many_inputs(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    response = _client(bridge).post(
        "/v1/images/edits", json={"prompt": "x", "images": ["A"] * 17},
        headers={"x-api-key": "bridge-secret"},
    )
    assert response.status_code == 400
    assert "16" in response.json["error"]["message"]


def test_edits_n_repeats_and_sums_input_image_tokens(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    calls = _post_stub(monkeypatch, [_Resp(200, _edit(1521)), _Resp(200, _edit(1521))])

    response = _client(bridge).post(
        "/v1/images/edits", json={"prompt": "x", "images": ["AAAA"], "n": 2},
        headers={"x-api-key": "bridge-secret"},
    )

    assert response.status_code == 200
    assert len(calls) == 2
    assert len(response.json["data"]) == 2
    assert response.json["usage"]["input_tokens_details"]["image_tokens"] == 3042


def test_generations_still_ignores_images(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "pool", _Pool("pro"))
    calls = _post_stub(monkeypatch, [_Resp(200, _image())])

    _client(bridge).post(
        "/v1/images/generations", json={"prompt": "x", "images": ["AAAA"]},
        headers={"x-api-key": "bridge-secret"},
    )

    assert calls[0]["url"] == f"{bridge.CODEX_BASE}/images/generations"
    assert "images" not in calls[0]["json"]
