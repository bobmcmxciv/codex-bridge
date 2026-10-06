# One-off: probe whether gpt-5.4 serves, and whether its extended (1M) context
# lane actually accepts beyond-272k prompts. Uses the pool's own credential
# refresh logic; run from ~/codex-bridge.
import json, sys, time, uuid

import requests

import pool_paths
from codex_accounts import AccountPool

CODEX_BASE = "https://chatgpt.com/backend-api/codex"
UA = "codex_cli_rs/0.146.0 (Windows 10.0.20348; x86_64)"

pool = AccountPool(pool_paths.ACCOUNTS_CONFIG, pool_paths.ACCOUNTS_STATE)
account = pool.active()
access, account_id = account.creds.get()
print("account:", account.id, file=sys.stderr)

LINE = "line %06d: the quick brown fox jumps over the lazy dog near the river bank at dawn.\n"

def probe(model, n_lines, tag):
    filler = "".join(LINE % i for i in range(n_lines))
    key = f"probe54-{tag}-{uuid.uuid4().hex[:8]}"
    payload = {
        "model": model,
        "instructions": "You are a helpful assistant.",
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": filler + "\nIgnore the lines above. Reply with exactly one word: OK"}]}],
        "tools": [], "tool_choice": "auto", "parallel_tool_calls": False,
        "store": False, "stream": True, "prompt_cache_key": key,
    }
    headers = {
        "Authorization": f"Bearer {access}", "chatgpt-account-id": account_id,
        "Content-Type": "application/json", "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental", "originator": "codex_cli_rs",
        "session_id": key, "User-Agent": UA,
    }
    t0 = time.time()
    r = requests.post(f"{CODEX_BASE}/responses", json=payload, headers=headers,
                      stream=True, timeout=240)
    print(f"{tag}: HTTP {r.status_code} in {time.time()-t0:.1f}s")
    if r.status_code != 200:
        print("  body:", r.text[:300])
        return
    got_text = []
    usage = None
    r.encoding = "utf-8"
    for raw in r.iter_lines(decode_unicode=True):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if not raw or not raw.startswith("data:"):
            continue
        p = raw[5:].strip()
        if p == "[DONE]":
            break
        try:
            evt = json.loads(p)
        except Exception:
            continue
        et = evt.get("type", "")
        if et in ("error", "response.failed"):
            err = evt.get("error") or (evt.get("response") or {}).get("error") or {}
            print("  REFUSED:", str(err)[:200]); r.close(); return
        if et == "response.output_text.delta":
            got_text.append(evt.get("delta", ""))
        if et == "response.completed":
            usage = (evt.get("response") or {}).get("usage") or {}
            break
    print(f"  OK in {time.time()-t0:.1f}s text={''.join(got_text)[:40]!r} usage={usage}")

if __name__ == "__main__":
    probe(sys.argv[1], int(sys.argv[2]), sys.argv[3])
