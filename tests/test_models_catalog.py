"""Model catalog: client-version discovery, per-account/version cache, /v1/models shape.

Background (2026-09-06): the upstream catalog is gated by `client_version`; a
pinned 0.146.0 never listed gpt-6-astra while 0.153.4 did, so the bridge now
discovers the installed CLI version and only pins when told to explicitly.
"""
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
_TMP = Path(tempfile.mkdtemp(prefix="codex-catalog-test-"))
os.environ["CODEX_ACCOUNTS_CONFIG"] = str(_TMP / "pool.json")
os.environ["CODEX_ACCOUNTS_STATE"] = str(_TMP / "state.json")
# Pin the version so importing the module never shells out to a real CLI.
os.environ["CODEX_CLIENT_VERSION"] = "9.9.9"
os.environ["CODEX_MODEL"] = "gpt-6-astra"
os.environ["CODEX_MODEL_ALLOWED"] = "gpt-6-astra,gpt-5.6-sol,config-only-slug"

import fake_auth  # noqa: E402,F401
import codex_bridge  # noqa: E402


class _Creds:
    def __init__(self, account_id: str):
        self.account_id = account_id

    def get(self, force_refresh: bool = False):
        return "access-token", self.account_id


class _Account:
    def __init__(self, account_id: str):
        self.id = account_id
        self.creds = _Creds(account_id)


class _Pool:
    def __init__(self, account_id: str):
        self.account = _Account(account_id)

    def active(self):
        return self.account


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


CATALOG = {
    "models": [
        {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list",
         "context_window": 272000, "max_context_window": 872000,
         "supported_reasoning_levels": [{"effort": "low"}, {"effort": "high"}, {"effort": "ultra"}]},
        {"slug": "gpt-5.6-sol", "display_name": "GPT-5.6-Sol", "visibility": "list",
         "context_window": 272000, "max_context_window": 272000},
        {"slug": "codex-auto-review", "display_name": "Codex Auto Review", "visibility": "hide",
         "context_window": 272000, "max_context_window": 272000},
    ]
}


@pytest.fixture()
def fresh_catalog(monkeypatch):
    codex_bridge._catalog_state.update(key=None, ts=0.0, next_try=0.0, models=[], error=None)
    # Another test module may have imported codex_bridge first (detecting the
    # real CLI version); pin the module-level values so assertions are stable.
    monkeypatch.setattr(codex_bridge, "CODEX_CLIENT_VERSION", "9.9.9")
    monkeypatch.setattr(codex_bridge, "CODEX_CLIENT_VERSION_SOURCE", "env:CODEX_CLIENT_VERSION")
    monkeypatch.setattr(codex_bridge, "CODEX_UA", "codex_cli_rs/9.9.9 (Windows 10.0.20348; x86_64)")
    monkeypatch.setattr(codex_bridge, "CODEX_MODEL", "gpt-6-astra")
    monkeypatch.setattr(codex_bridge, "CODEX_MODEL_ALLOWED", {"gpt-6-astra", "gpt-5.6-sol", "config-only-slug"})
    monkeypatch.setattr(codex_bridge, "pool", _Pool("acct-A"))
    calls: list[dict] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        return _Resp(200, CATALOG)

    monkeypatch.setattr(codex_bridge.requests, "get", fake_get)
    yield calls
    codex_bridge._catalog_state.update(key=None, ts=0.0, next_try=0.0, models=[], error=None)


# --- client_version discovery -------------------------------------------------

def test_env_pin_wins_over_everything(monkeypatch):
    monkeypatch.setenv("CODEX_CLIENT_VERSION", "0.146.0")
    monkeypatch.setattr(codex_bridge, "_version_from_cli", lambda cmd: "0.153.4")
    assert codex_bridge._detect_codex_client_version() == ("0.146.0", "env:CODEX_CLIENT_VERSION")


def test_installed_cli_version_is_used_when_not_pinned(monkeypatch):
    monkeypatch.delenv("CODEX_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setattr(codex_bridge, "_version_from_cli", lambda cmd: "0.153.4" if cmd == "codex" else None)
    assert codex_bridge._detect_codex_client_version() == ("0.153.4", "cli:PATH")


def test_models_cache_witnesses_the_cli_when_path_lacks_codex(monkeypatch, tmp_path):
    # The service runs as LocalSystem without the user's PATH; the CLI stamps
    # its version into CODEX_HOME/models_cache.json on every refresh.
    monkeypatch.delenv("CODEX_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({"client_version": "0.153.4", "models": []}))
    monkeypatch.setattr(codex_bridge, "_version_from_cli", lambda cmd: None)
    assert codex_bridge._detect_codex_client_version() == ("0.153.4", "models_cache.json")


def test_baseline_is_last_resort(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_CLIENT_VERSION", raising=False)
    monkeypatch.delenv("CODEX_CLI_PATH", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))       # no models_cache.json here
    monkeypatch.setenv("APPDATA", str(tmp_path))          # no npm package here either
    monkeypatch.setattr(codex_bridge, "_version_from_cli", lambda cmd: None)
    version, source = codex_bridge._detect_codex_client_version()
    assert (version, source) == (codex_bridge._CODEX_CLIENT_VERSION_BASELINE, "baseline")
    # The baseline must be a version that already lists gpt-6-astra upstream.
    assert tuple(int(p) for p in version.split(".")) >= (0, 153, 4)


def test_version_parsed_from_cli_banner(monkeypatch):
    import subprocess

    class Out:
        stdout = "codex-cli 0.153.4\n"
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Out())
    assert codex_bridge._version_from_cli("codex") == "0.153.4"


def test_version_from_cli_is_none_when_the_cli_is_missing(monkeypatch):
    import subprocess

    def missing(*a, **k):
        raise FileNotFoundError("codex")

    monkeypatch.setattr(subprocess, "run", missing)
    assert codex_bridge._version_from_cli("codex") is None


# --- catalog cache --------------------------------------------------------------

def test_catalog_requests_the_detected_client_version(fresh_catalog):
    snap = codex_bridge._catalog_snapshot()
    assert [m["slug"] for m in snap["models"]] == ["gpt-6-astra", "gpt-5.6-sol", "codex-auto-review"]
    assert fresh_catalog[0]["params"] == {"client_version": "9.9.9"}
    assert fresh_catalog[0]["headers"]["User-Agent"].startswith("codex_cli_rs/9.9.9")
    assert snap["stale"] is False and snap["error"] is None


def test_catalog_is_cached_per_account_and_refetched_after_failover(fresh_catalog, monkeypatch):
    codex_bridge._catalog_snapshot()
    codex_bridge._catalog_snapshot()
    assert len(fresh_catalog) == 1, "second call within TTL must hit the cache"
    # Pool failover to another account: its catalog may differ, so refetch.
    monkeypatch.setattr(codex_bridge, "pool", _Pool("acct-B"))
    snap = codex_bridge._catalog_snapshot()
    assert len(fresh_catalog) == 2
    assert snap["key"] == ("acct-B", "9.9.9")


def test_catalog_refresh_flag_bypasses_ttl(fresh_catalog):
    codex_bridge._catalog_snapshot()
    codex_bridge._catalog_snapshot(force=True)
    assert len(fresh_catalog) == 2


def test_catalog_keeps_last_snapshot_and_reports_stale_on_failure(fresh_catalog, monkeypatch):
    good = codex_bridge._catalog_snapshot()
    assert good["models"]

    def broken_get(*a, **k):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(codex_bridge.requests, "get", broken_get)
    snap = codex_bridge._catalog_snapshot(force=True)
    assert [m["slug"] for m in snap["models"]] == [m["slug"] for m in good["models"]]
    assert snap["stale"] is True
    assert "upstream down" in (snap["error"] or "")
    # Allowlist still includes the last good catalog while stale.
    assert "gpt-6-astra" in codex_bridge._allowed_models()
    # Failures back off instead of hammering upstream on every request.
    codex_bridge._catalog_snapshot()
    assert codex_bridge._catalog_state["next_try"] > time.time()


# --- /v1/models -----------------------------------------------------------------

def test_models_route_exposes_capabilities_and_marks_config_only_entries(fresh_catalog):
    client = codex_bridge.app.test_client()
    body = client.get("/v1/models").get_json()
    assert body["object"] == "list"
    assert body["default_model"] == "gpt-6-astra"
    assert body["client_version"] == "9.9.9"
    assert body["client_version_source"] == "env:CODEX_CLIENT_VERSION"
    assert body["catalog_stale"] is False and body["catalog_error"] is None
    assert body["catalog_fetched_at"]

    by_id = {m["id"]: m for m in body["data"]}
    # Default first, hidden slugs excluded from the listing.
    assert body["data"][0]["id"] == "gpt-6-astra"
    assert "codex-auto-review" not in by_id
    astra = by_id["gpt-6-astra"]
    assert astra["is_default"] is True
    assert astra["source"] == "catalog"
    assert astra["reasoning_efforts"] == ["low", "high", "ultra"]
    assert astra["context_window"] == 272000 and astra["max_context_window"] == 872000
    # Allowlisted slug the catalog did not list: advertised, but honestly tagged.
    cfg = by_id["config-only-slug"]
    assert cfg["source"] == "config" and cfg["is_default"] is False
    assert "context_window" not in cfg


def test_models_route_refresh_param_forces_a_fetch(fresh_catalog):
    client = codex_bridge.app.test_client()
    client.get("/v1/models")
    client.get("/v1/models")
    assert len(fresh_catalog) == 1
    client.get("/v1/models?refresh=1")
    assert len(fresh_catalog) == 2


def test_health_reports_client_version(fresh_catalog):
    body = codex_bridge.app.test_client().get("/health").get_json()
    assert body["client_version"] == "9.9.9"
    assert body["client_version_source"] == "env:CODEX_CLIENT_VERSION"


# --- client version re-detection (2026-09-30) -----------------------------------

def _detected(monkeypatch, version, source="cli:CODEX_CLI_PATH"):
    monkeypatch.setattr(codex_bridge, "_detect_codex_client_version", lambda: (version, source))
    monkeypatch.setattr(codex_bridge, "CODEX_CLIENT_VERSION", "0.156.0")
    monkeypatch.setattr(codex_bridge, "CODEX_CLIENT_VERSION_SOURCE", "models_cache.json")
    monkeypatch.setattr(codex_bridge, "CODEX_UA", "codex_cli_rs/0.156.0 (Windows 10.0.20348; x86_64)")
    monkeypatch.setitem(codex_bridge._version_state, "checked_at", 0.0)


def test_cli_upgrade_reaches_the_catalog_without_restart(fresh_catalog, monkeypatch):
    _detected(monkeypatch, "0.156.0")
    codex_bridge._catalog_snapshot()
    assert fresh_catalog[-1]["params"] == {"client_version": "0.156.0"}
    # `npm i -g @openai/codex` lands 0.159.2; the next due recheck adopts it and
    # the (account, version) cache key forces a refetch at the new version.
    monkeypatch.setattr(codex_bridge, "_detect_codex_client_version", lambda: ("0.159.2", "cli:CODEX_CLI_PATH"))
    monkeypatch.setitem(codex_bridge._version_state, "checked_at", 0.0)
    snap = codex_bridge._catalog_snapshot()
    assert fresh_catalog[-1]["params"] == {"client_version": "0.159.2"}
    assert fresh_catalog[-1]["headers"]["User-Agent"].startswith("codex_cli_rs/0.159.2")
    assert snap["key"] == ("acct-A", "0.159.2")
    assert codex_bridge.CODEX_CLIENT_VERSION_SOURCE == "cli:CODEX_CLI_PATH"


def test_recheck_never_downgrades_on_a_stale_fallback(monkeypatch):
    _detected(monkeypatch, "0.150.0", "models_cache.json")
    assert codex_bridge._recheck_client_version() is False
    assert codex_bridge.CODEX_CLIENT_VERSION == "0.156.0"


def test_explicit_pin_is_adopted_even_if_lower(monkeypatch):
    _detected(monkeypatch, "0.146.0", "env:CODEX_CLIENT_VERSION")
    assert codex_bridge._recheck_client_version() is True
    assert codex_bridge.CODEX_CLIENT_VERSION == "0.146.0"


def test_recheck_is_rate_limited(monkeypatch):
    _detected(monkeypatch, "0.159.2")
    monkeypatch.setitem(codex_bridge._version_state, "checked_at", time.time())
    assert codex_bridge._recheck_client_version() is False
    assert codex_bridge._recheck_client_version(force=True) is True
    assert codex_bridge.CODEX_CLIENT_VERSION == "0.159.2"


def test_explicit_user_agent_override_survives_upgrade(monkeypatch):
    _detected(monkeypatch, "0.159.2")
    monkeypatch.setenv("CODEX_USER_AGENT", "custom-ua")
    monkeypatch.setattr(codex_bridge, "CODEX_UA", "custom-ua")
    assert codex_bridge._recheck_client_version() is True
    assert codex_bridge.CODEX_UA == "custom-ua"
