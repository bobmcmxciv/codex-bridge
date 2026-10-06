"""What does GET /backend-api/wham/usage really return for a login?

    python probe_usage.py [pool-account-id]

Defaults to the pool account that would serve the next request.
"""
import json
import sys

import requests

from codex_accounts import AccountPool
from pool_paths import ACCOUNTS_CONFIG, ACCOUNTS_STATE

WANTED = sys.argv[1] if len(sys.argv) > 1 else ""
pool = AccountPool(ACCOUNTS_CONFIG, ACCOUNTS_STATE)
account = next((a for a in pool.accounts() if a.id == WANTED), None) if WANTED else pool.active()
if account is None:
    raise SystemExit(f"no account {WANTED!r} in the pool")
print(f"account={account.id}")
access, account_id = account.creds.get()
resp = requests.get(
    "https://chatgpt.com/backend-api/wham/usage",
    headers={
        "Authorization": f"Bearer {access}",
        "chatgpt-account-id": account_id,
        "User-Agent": "codex_cli_rs/0.145.0 (Windows 10.0.20348; x86_64)",
        "originator": "codex_cli_rs",
    },
    timeout=30,
)
print("HTTP", resp.status_code)
body = resp.json()
# Redact nothing structural; this payload holds percentages and timestamps only.
print(json.dumps(body, indent=2, ensure_ascii=False)[:4000])
