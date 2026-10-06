"""Context-ceiling probe: does <model> accept an N-line filler prompt?

    python probe_ctx.py <model> <n_lines> <tag> [account-id]

Reports HTTP status, in-stream refusal (context_length_exceeded), or the
usage block with the real input_tokens count so the next size can be aimed.
Reasoning effort is forced low to keep output (8x-weighted) negligible.
"""
import json, sys, time, uuid
import requests
import pool_paths
from codex_accounts import AccountPool

CODEX_BASE = "https://chatgpt.com/backend-api/codex"
UA = "codex_cli_rs/0.153.2 (Windows 10.0.20348; x86_64)"
model, n_lines, tag = sys.argv[1], int(sys.argv[2]), sys.argv[3]
want = sys.argv[4] if len(sys.argv) > 4 else "pro-cad25176"
pool = AccountPool(pool_paths.ACCOUNTS_CONFIG, pool_paths.ACCOUNTS_STATE)
account = next(a for a in pool.accounts() if a.id == want)
access, account_id = account.creds.get()
LINE = "line %06d: the quick brown fox jumps over the lazy dog near the river bank at dawn.\n"
filler = "".join(LINE % i for i in range(n_lines))
key = f"probe-ctx-{tag}-{uuid.uuid4().hex[:8]}"
payload = {
    "model": model, "instructions": "You are a helpful assistant.",
    "input": [{"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": filler + "\nIgnore the lines above. Reply with exactly one word: OK"}]}],
    "reasoning": {"effort": "low"},
    "tools": [], "tool_choice": "auto", "parallel_tool_calls": False,
    "store": False, "stream": True, "prompt_cache_key": key,
}
headers = {"Authorization": f"Bearer {access}", "chatgpt-account-id": account_id,
           "Content-Type": "application/json", "Accept": "text/event-stream",
           "OpenAI-Beta": "responses=experimental", "originator": "codex_cli_rs",
           "session_id": key, "User-Agent": UA}
t0 = time.time()
r = requests.post(f"{CODEX_BASE}/responses", json=payload, headers=headers, stream=True, timeout=600)
print(f"{tag}: model={model} lines={n_lines} chars={len(filler):,} -> HTTP {r.status_code} in {time.time()-t0:.1f}s")
if r.status_code != 200:
    print("  body:", r.text[:400]); sys.exit(2)
got, usage, refused = [], None, None
for raw in r.iter_lines():
    line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
    if not line.startswith("data:"): continue
    p = line[5:].strip()
    if p == "[DONE]": break
    try: evt = json.loads(p)
    except Exception: continue
    et = evt.get("type", "")
    if et in ("error", "response.failed"):
        err = evt.get("error") or (evt.get("response") or {}).get("error") or {}
        refused = err; break
    if et == "response.output_text.delta": got.append(evt.get("delta", ""))
    if et == "response.completed":
        usage = (evt.get("response") or {}).get("usage") or {}; break
r.close()
if refused is not None:
    print(f"  REFUSED in {time.time()-t0:.1f}s: code={refused.get('code')} msg={str(refused.get('message'))[:220]}"); sys.exit(3)
it = (usage or {}).get("input_tokens"); ot = (usage or {}).get("output_tokens")
print(f"  OK in {time.time()-t0:.1f}s text={''.join(got)[:20]!r} input_tokens={it:,} output_tokens={ot} tokens/line={it/n_lines:.2f}" if it else f"  OK but no usage: {usage}")
