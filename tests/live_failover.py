"""End-to-end failover check over real HTTP, against a stub upstream.

Starts a stub Codex backend and a second, throwaway codex-bridge process (own
port, own pool, own state file), then drives real requests through it:

  1. account-1's subscription is spent  -> the request must still answer 200,
     served by account-2, with account-1 parked until its reset time
  2. the next request must go straight to account-2, no wasted 429 round trip
  3. /accounts and /usage must report the switch
  4. once account-1's window is released, it must take over again

Nothing here touches the live services, ~/.codex, or chatgpt.com.

    python tests/live_failover.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
APP_DIR = HERE.parent
sys.path.insert(0, str(HERE))
import fake_auth  # noqa: E402

PY = sys.executable
STUB_PORT = int(os.environ.get("STUB_PORT", "8913"))
BRIDGE_PORT = int(os.environ.get("BRIDGE_PORT", "8912"))
TOKEN = "live-failover-test-token"
WORK = HERE / ".live-failover"
CALLS = WORK / "stub-calls.log"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


def wait_for(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(url, timeout=2)
            return True
        except requests.RequestException:
            time.sleep(0.3)
    return False


def labels_seen(kind: str = "responses") -> list[str]:
    if not CALLS.exists():
        return []
    out = []
    for line in CALLS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        evt = json.loads(line)
        if evt["kind"] == kind:
            out.append(evt["label"])
    return out


def ask(prompt: str) -> requests.Response:
    return requests.post(
        f"http://127.0.0.1:{BRIDGE_PORT}/v1/chat/completions",
        headers={"x-api-key": TOKEN},
        json={"model": "claude-sonnet-5", "stream": False,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=30,
    )


def main() -> int:
    WORK.mkdir(exist_ok=True)
    fake_auth.write(WORK / "acct1.json", "acct1", plan="prolite")
    fake_auth.write(WORK / "acct2.json", "acct2", plan="pro")
    pool_path = WORK / "pool.json"
    state_path = WORK / "state.json"
    pool_path.write_text(json.dumps({"accounts": [
        {"id": "primary-spent", "path": str(WORK / "acct1.json")},
        {"id": "backup-fresh", "path": str(WORK / "acct2.json")},
    ]}, indent=2), encoding="utf-8")
    state_path.unlink(missing_ok=True)
    CALLS.write_text("", encoding="utf-8")

    stub_env = {**os.environ, "STUB_PORT": str(STUB_PORT), "STUB_QUOTA_SPENT": "acct1",
                "STUB_CALLS": str(CALLS)}
    bridge_env = {**os.environ,
                  "CODEX_BASE_URL": f"http://127.0.0.1:{STUB_PORT}",
                  "CODEX_USAGE_URL": f"http://127.0.0.1:{STUB_PORT}/wham/usage",
                  "CODEX_BRIDGE_PORT": str(BRIDGE_PORT),
                  "CODEX_BRIDGE_TOKEN": TOKEN,
                  "CODEX_ACCOUNTS_CONFIG": str(pool_path),
                  "CODEX_ACCOUNTS_STATE": str(state_path),
                  "PYTHONUNBUFFERED": "1"}

    procs = []
    try:
        procs.append(subprocess.Popen([PY, str(HERE / "stub_upstream.py")], env=stub_env,
                                      cwd=str(APP_DIR)))
        procs.append(subprocess.Popen([PY, str(APP_DIR / "codex_bridge.py")], env=bridge_env,
                                      cwd=str(APP_DIR)))
        if not wait_for(f"http://127.0.0.1:{STUB_PORT}/wham/usage"):
            check("stub upstream came up", False)
            return 1
        if not wait_for(f"http://127.0.0.1:{BRIDGE_PORT}/health"):
            check("test bridge came up", False)
            return 1

        # 1. the spent primary must not surface as an error
        r = ask("hello")
        body = r.json() if r.status_code == 200 else {}
        content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        check("request survives a spent primary", r.status_code == 200,
              f"HTTP {r.status_code}")
        check("answer came from the backup account", content == "served-by-acct2",
              f"content={content!r}")
        check("primary was tried first, then the backup",
              labels_seen() == ["acct1", "acct2"], f"upstream saw {labels_seen()}")

        state = json.loads(state_path.read_text(encoding="utf-8"))
        parked = state.get("primary-spent") or {}
        check("spent account parked as usage_limit_reached",
              parked.get("reason") == "usage_limit_reached", json.dumps(parked)[:120])
        check("park lasts until the upstream's reset time",
              4000 < parked.get("cooldown_until", 0) - time.time() <= 5000,
              f"{parked.get('cooldown_until', 0) - time.time():.0f}s left")

        # 2. no repeated 429 round trip on the parked account
        r2 = ask("again")
        check("second request skips the parked account", labels_seen() == ["acct1", "acct2", "acct2"],
              f"upstream saw {labels_seen()}")
        check("second request still 200", r2.status_code == 200, f"HTTP {r2.status_code}")

        # 3. reporting
        acc = requests.get(f"http://127.0.0.1:{BRIDGE_PORT}/accounts",
                           headers={"x-api-key": TOKEN}, timeout=15).json()
        by_id = {a["id"]: a for a in acc["accounts"]}
        check("/accounts marks the backup active", acc.get("active") == "backup-fresh",
              json.dumps(acc.get("active")))
        check("/accounts shows the primary parked with a reason",
              by_id["primary-spent"]["available"] is False
              and by_id["primary-spent"]["cooldown_reason"] == "usage_limit_reached")
        check("/accounts reports per-account usage",
              by_id["primary-spent"].get("used_percent") == 100
              and by_id["backup-fresh"].get("used_percent") == 4,
              f"{by_id['primary-spent'].get('used_percent')} / {by_id['backup-fresh'].get('used_percent')}")
        usage = requests.get(f"http://127.0.0.1:{BRIDGE_PORT}/usage",
                             headers={"x-api-key": TOKEN}, timeout=15).json()
        check("/usage follows the account in service",
              usage.get("user_id") == "user-acct2", json.dumps(usage.get("user_id")))
        health = requests.get(f"http://127.0.0.1:{BRIDGE_PORT}/health", timeout=15).json()
        check("/health names the account in service", health.get("account") == "backup-fresh",
              json.dumps(health.get("account")))

        # 4. recovery: window lifts -> primary takes over again, live
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["primary-spent"]["cooldown_until"] = int(time.time()) - 1
        state_path.write_text(json.dumps(state), encoding="utf-8")
        # the stub's quota is refilled too, so the primary can actually serve
        procs[0].kill()
        procs[0] = subprocess.Popen([PY, str(HERE / "stub_upstream.py")],
                                    env={**stub_env, "STUB_QUOTA_SPENT": ""}, cwd=str(APP_DIR))
        wait_for(f"http://127.0.0.1:{STUB_PORT}/wham/usage")
        before = len(labels_seen())
        r3 = ask("after the reset")
        content3 = (((r3.json().get("choices") or [{}])[0].get("message") or {}).get("content") or "")
        check("primary takes over once its window resets", content3 == "served-by-acct1",
              f"content={content3!r}, upstream saw {labels_seen()[before:]}")
    finally:
        for p in procs:
            p.kill()

    print()
    if failures:
        print(f"{len(failures)} check(s) FAILED: {', '.join(failures)}")
        return 1
    print("all failover checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
