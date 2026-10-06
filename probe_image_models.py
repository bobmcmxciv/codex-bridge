"""Probe which image model ids the ChatGPT (Codex) backend accepts.

Sends a minimal generations request per candidate model to
{CODEX_BASE}/images/generations with the same headers codex_bridge uses.
A negative control (bogus model) shows what an unsupported model looks like.
"""
import json, sys, time, requests, pool_paths
from codex_accounts import AccountPool

CODEX_BASE = "https://chatgpt.com/backend-api/codex"
CLIENT_VERSION = "0.153.4"
UA = f"codex_cli_rs/{CLIENT_VERSION} (Windows 10.0.20348; x86_64)"

pool = AccountPool(pool_paths.ACCOUNTS_CONFIG, pool_paths.ACCOUNTS_STATE)
account = pool.active()
access, account_id = account.creds.get()
print(f"# account={account.id} plan-note={account.__dict__.get('note','')[:40]!r}", flush=True)

H = {
    "Authorization": f"Bearer {access}",
    "chatgpt-account-id": account_id,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "originator": "codex_cli_rs",
    "version": CLIENT_VERSION,
    "User-Agent": UA,
}

candidates = sys.argv[1:] or [
    "gpt-image-9-doesnotexist",   # negative control
    "gpt-image-2",                # positive control (current pin)
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5",
]

for model in candidates:
    body = {
        "prompt": "a single small red circle centered on a white background",
        "model": model,
        "size": "1024x1024",
        "quality": "low",
        "background": "auto",
    }
    t0 = time.time()
    try:
        r = requests.post(f"{CODEX_BASE}/images/generations", json=body, headers=H, timeout=240)
    except Exception as e:
        print(f"{model:28s} EXC  {type(e).__name__}: {e}", flush=True)
        continue
    dt = time.time() - t0
    if r.status_code == 200:
        try:
            d = r.json()
        except Exception:
            print(f"{model:28s} 200  {dt:5.1f}s  (non-JSON, {len(r.content)}B)", flush=True)
            continue
        data = d.get("data") or []
        b64 = (data[0] or {}).get("b64_json", "") if data else ""
        print(f"{model:28s} 200  {dt:5.1f}s  b64={len(b64)}B size={d.get('size')} "
              f"quality={d.get('quality')} usage={json.dumps(d.get('usage'))}", flush=True)
        if b64:
            import base64
            fn = f"/tmp/probe_{model.replace('.','_')}.png"
            open(fn, "wb").write(base64.b64decode(b64))
            print(f"{'':28s}      -> {fn}", flush=True)
    else:
        print(f"{model:28s} {r.status_code}  {dt:5.1f}s  {r.text[:300]}", flush=True)
