"""Probe the ChatGPT Codex Responses endpoint with a Codex OAuth login.

    python probe.py [model] [pool-account-id]

Defaults to the pool account that would serve the next request; pass an id from
`manage_accounts.py list` to probe a specific subscription instead.
"""
from __future__ import annotations

import json
import sys
import uuid

import requests

from codex_accounts import AccountPool
from pool_paths import ACCOUNTS_CONFIG, ACCOUNTS_STATE

BASE = "https://chatgpt.com/backend-api/codex"
MODEL = sys.argv[1] if len(sys.argv) > 1 else "gpt-5.6-sol"
WANTED = sys.argv[2] if len(sys.argv) > 2 else ""

pool = AccountPool(ACCOUNTS_CONFIG, ACCOUNTS_STATE)
account = next((a for a in pool.accounts() if a.id == WANTED), None) if WANTED else pool.active()
if account is None:
    raise SystemExit(f"no account {WANTED!r} in the pool")
access, account_id = account.creds.get()
print(f"account={account.id} plan={account.creds.plan_type()} "
      f"account_id={account_id[:8]}... token_len={len(access)}")

body = {
    "model": MODEL,
    "instructions": "You are a terse assistant.",
    "input": [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "reply with exactly: PONG"}],
        }
    ],
    "tools": [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ],
    "tool_choice": "auto",
    "parallel_tool_calls": False,
    "store": False,
    "stream": True,
}

headers = {
    "Authorization": f"Bearer {access}",
    "chatgpt-account-id": account_id,
    "Content-Type": "application/json",
    "Accept": "text/event-stream",
    "OpenAI-Beta": "responses=experimental",
    "originator": "codex_cli_rs",
    "session_id": str(uuid.uuid4()),
    "User-Agent": "codex_cli_rs/0.145.0 (Windows 10.0.20348; x86_64)",
}

r = requests.post(f"{BASE}/responses", json=body, headers=headers, stream=True, timeout=120)
print("HTTP", r.status_code)
print("resp-headers:", {k: v for k, v in r.headers.items() if k.lower() in
                        ("content-type", "x-request-id", "retry-after")})

if r.status_code != 200:
    print("BODY:", r.text[:1500])
    raise SystemExit(1)

seen_types: list[str] = []
text_out: list[str] = []
r.encoding = "utf-8"
for raw in r.iter_lines(decode_unicode=True):
    if not raw or not raw.startswith("data:"):
        continue
    payload = raw[5:].strip()
    if payload == "[DONE]":
        seen_types.append("[DONE]")
        break
    try:
        evt = json.loads(payload)
    except json.JSONDecodeError:
        continue
    et = evt.get("type", "?")
    if et not in seen_types:
        seen_types.append(et)
    if et == "response.output_text.delta":
        text_out.append(evt.get("delta", ""))
    if et == "response.completed":
        usage = (evt.get("response") or {}).get("usage")
        print("USAGE:", json.dumps(usage))

print("EVENT TYPES:", seen_types)
print("TEXT:", "".join(text_out).strip())
