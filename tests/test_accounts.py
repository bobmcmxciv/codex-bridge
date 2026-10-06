"""Pool selection, cooldown bookkeeping, and the account-switching retry loop."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# codex_bridge builds its pool at import time; point it somewhere disposable.
_TMP = Path(tempfile.mkdtemp(prefix="codex-pool-test-"))
os.environ["CODEX_ACCOUNTS_CONFIG"] = str(_TMP / "pool.json")
os.environ["CODEX_ACCOUNTS_STATE"] = str(_TMP / "state.json")

import fake_auth  # noqa: E402
import codex_bridge  # noqa: E402
from codex_accounts import AccountPool  # noqa: E402


@pytest.fixture()
def pool(tmp_path):
    fake_auth.write(tmp_path / "a1.json", "acct1", plan="prolite")
    fake_auth.write(tmp_path / "a2.json", "acct2", plan="pro")
    config = tmp_path / "pool.json"
    config.write_text(json.dumps({"accounts": [
        {"id": "first", "path": str(tmp_path / "a1.json")},
        {"id": "second", "path": str(tmp_path / "a2.json")},
    ]}), encoding="utf-8")
    return AccountPool(config, tmp_path / "state.json")


def test_priority_is_config_order(pool):
    assert [a.id for a in pool.candidates()] == ["first", "second"]
    assert pool.active().id == "first"


def test_quota_park_moves_account_to_the_back_and_persists(pool):
    first = pool.active()
    resets_at = time.time() + 3600
    pool.mark_quota(first, resets_at=resets_at, detail="usage_limit_reached")

    assert pool.active().id == "second"
    # A parked account stays as a last resort rather than disappearing.
    assert [a.id for a in pool.candidates()] == ["second", "first"]

    state = json.loads((pool.state_path).read_text(encoding="utf-8"))
    assert state["first"]["reason"] == "usage_limit_reached"
    assert state["first"]["cooldown_until"] == int(resets_at)

    # A fresh pool over the same files keeps the parking - service restarts and
    # separate processes (manage_accounts.py) must agree.
    reloaded = AccountPool(pool.config_path, pool.state_path)
    assert reloaded.active().id == "second"


def test_stale_resets_at_falls_back_to_the_default_cooldown(pool):
    # A reset time already in the past tells us nothing about when the window
    # really lifts, so the account is parked for the default hour instead of
    # being retried on every request.
    pool.mark_quota(pool.active(), resets_at=time.time() - 1)
    assert pool.active().id == "second"
    left = pool.describe_one(pool.accounts()[0])["cooldown_seconds_left"]
    assert 3500 <= left <= 3600


def test_expired_cooldown_returns_the_primary(pool):
    pool.mark_quota(pool.active(), resets_at=time.time() + 3600)
    state = json.loads(pool.state_path.read_text(encoding="utf-8"))
    state["first"]["cooldown_until"] = int(time.time()) - 1
    pool.state_path.write_text(json.dumps(state), encoding="utf-8")

    reloaded = AccountPool(pool.config_path, pool.state_path)
    assert reloaded.active().id == "first"


def test_bogus_resets_at_is_capped(pool):
    pool.mark_quota(pool.active(), resets_at=time.time() + 400 * 24 * 3600)
    left = pool.describe_one(pool.accounts()[0])["cooldown_seconds_left"]
    assert left <= 8 * 24 * 3600


def test_usage_payload_parks_and_releases(pool):
    first = pool.accounts()[0]
    reset_at = int(time.time() + 7200)
    pool.note_usage(first, {"rate_limit": {"limit_reached": True,
                                           "primary_window": {"reset_at": reset_at}}})
    assert pool.active().id == "second"

    pool.note_usage(first, {"rate_limit": {"limit_reached": False,
                                           "primary_window": {"used_percent": 3}}})
    assert pool.active().id == "first"


def test_identity_mismatch_is_reported(tmp_path):
    fake_auth.write(tmp_path / "a.json", "acctX")
    config = tmp_path / "pool.json"
    config.write_text(json.dumps({"accounts": [
        {"id": "x", "path": str(tmp_path / "a.json"), "account_id": "someone-else"},
    ]}), encoding="utf-8")
    info = AccountPool(config, tmp_path / "state.json").describe()[0]
    assert info["identity_mismatch"] is True


def test_missing_config_falls_back_to_the_single_login(tmp_path):
    single = AccountPool(tmp_path / "nope.json", tmp_path / "state.json")
    assert len(single.accounts()) == 1


# -- the retry loop ---------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code: int, text: str = "", label: str = ""):
        self.status_code = status_code
        self.text = text
        self.label = label
        self.encoding = None
        self.closed = False

    def close(self):
        self.closed = True


QUOTA_BODY = json.dumps({"error": {
    "type": "usage_limit_reached", "message": "The usage limit has been reached",
    "plan_type": "prolite", "resets_at": int(time.time()) + 5000,
    "resets_in_seconds": 5000}})


def _install(monkeypatch, pool, responder):
    monkeypatch.setattr(codex_bridge, "pool", pool)
    calls: list[str] = []

    def fake_post(url, json=None, headers=None, **kwargs):
        label = fake_auth.label_of(headers["Authorization"].removeprefix("Bearer "))
        calls.append(label)
        return responder(label)

    monkeypatch.setattr(codex_bridge.requests, "post", fake_post)
    return calls


def test_quota_on_the_primary_switches_to_the_next_account(monkeypatch, pool):
    calls = _install(monkeypatch, pool, lambda label: (
        FakeResponse(429, QUOTA_BODY) if label == "acct1" else FakeResponse(200, label=label)
    ))

    resp, account = codex_bridge.call_upstream({"input": [], "instructions": ""})

    assert resp.status_code == 200
    assert account.id == "second"
    assert calls == ["acct1", "acct2"]
    # The spent account is parked, so the next request skips it outright.
    assert pool.active().id == "second"
    resp2, account2 = codex_bridge.call_upstream({"input": [], "instructions": ""})
    assert account2.id == "second" and calls == ["acct1", "acct2", "acct2"]


def test_throttling_429_parks_only_briefly(monkeypatch, pool):
    body = json.dumps({"error": {"type": "rate_limit_exceeded", "message": "slow down"}})
    _install(monkeypatch, pool, lambda label: (
        FakeResponse(429, body) if label == "acct1" else FakeResponse(200, label=label)
    ))

    resp, account = codex_bridge.call_upstream({"input": [], "instructions": ""})
    assert resp.status_code == 200 and account.id == "second"
    info = pool.describe_one(pool.accounts()[0])
    assert info["cooldown_reason"] == "throttled"
    assert info["cooldown_seconds_left"] <= 60


def test_401_forces_one_refresh_then_moves_on(monkeypatch, pool):
    def responder(label):
        return FakeResponse(401, "nope") if label == "acct1" else FakeResponse(200, label=label)

    calls = _install(monkeypatch, pool, responder)
    # Refreshing must not reach the network; pretend it worked and changed nothing.
    monkeypatch.setattr(type(pool.accounts()[0].creds), "_refresh_locked", lambda self: None)

    resp, account = codex_bridge.call_upstream({"input": [], "instructions": ""})
    assert account.id == "second"
    assert calls == ["acct1", "acct1", "acct2"]  # one forced-refresh retry, then switch
    assert pool.describe_one(pool.accounts()[0])["cooldown_reason"] == "auth_rejected"


def test_request_errors_are_not_blamed_on_the_account(monkeypatch, pool):
    calls = _install(monkeypatch, pool, lambda label: FakeResponse(400, "bad request"))

    resp, account = codex_bridge.call_upstream({"input": [], "instructions": ""})
    assert resp.status_code == 400
    assert calls == ["acct1"]           # no pointless replay on the other account
    assert pool.active().id == "first"  # and nothing parked


def test_every_account_spent_returns_the_last_upstream_error(monkeypatch, pool):
    calls = _install(monkeypatch, pool, lambda label: FakeResponse(429, QUOTA_BODY))

    resp, account = codex_bridge.call_upstream({"input": [], "instructions": ""})
    assert resp.status_code == 429
    assert "usage_limit_reached" in resp.text
    assert calls == ["acct1", "acct2"]
    assert account is None


def test_free_plan_accounts_are_skipped(tmp_path):
    fake_auth.write(tmp_path / "a1.json", "acct1", plan="free")
    fake_auth.write(tmp_path / "a2.json", "acct2", plan="pro")
    config = tmp_path / "pool.json"
    config.write_text(json.dumps({"accounts": [
        {"id": "first", "path": str(tmp_path / "a1.json")},
        {"id": "second", "path": str(tmp_path / "a2.json")},
    ]}), encoding="utf-8")
    pool = AccountPool(config, tmp_path / "state.json")
    assert [a.id for a in pool.candidates()] == ["second"]
    assert pool.active().id == "second"
    first = next(d for d in pool.describe() if d["id"] == "first")
    assert first["available"] is False and "free" in first["skipped"]


def test_free_plan_is_used_only_when_nothing_else_exists(tmp_path):
    fake_auth.write(tmp_path / "a1.json", "acct1", plan="free")
    config = tmp_path / "pool.json"
    config.write_text(json.dumps({"accounts": [
        {"id": "only", "path": str(tmp_path / "a1.json")},
    ]}), encoding="utf-8")
    pool = AccountPool(config, tmp_path / "state.json")
    assert [a.id for a in pool.candidates()] == ["only"]


def test_plan_change_on_disk_is_picked_up(tmp_path):
    fake_auth.write(tmp_path / "a1.json", "acct1", plan="free")
    fake_auth.write(tmp_path / "a2.json", "acct2", plan="pro")
    config = tmp_path / "pool.json"
    config.write_text(json.dumps({"accounts": [
        {"id": "first", "path": str(tmp_path / "a1.json")},
        {"id": "second", "path": str(tmp_path / "a2.json")},
    ]}), encoding="utf-8")
    pool = AccountPool(config, tmp_path / "state.json")
    assert [a.id for a in pool.candidates()] == ["second"]
    fake_auth.write(tmp_path / "a1.json", "acct1", plan="prolite")
    os.utime(tmp_path / "a1.json", (time.time() + 5, time.time() + 5))
    assert [a.id for a in pool.candidates()] == ["first", "second"]
