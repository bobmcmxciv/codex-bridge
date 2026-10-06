"""Inspect and (optionally) consume rate-limit reset credits for a pool account.

    python probe_reset_credits.py <pool-account-id>            # list credits
    python probe_reset_credits.py <pool-account-id> --consume  # redeem one

Endpoints recovered from codex.exe 0.145.0 strings:
    GET  /backend-api/wham/rate-limit-reset-credits
    POST /backend-api/wham/rate-limit-reset-credits/consume
"""
import json
import sys
import uuid

import requests

from codex_accounts import AccountPool
from pool_paths import ACCOUNTS_CONFIG, ACCOUNTS_STATE

WANTED = sys.argv[1]
DO_CONSUME = "--consume" in sys.argv[2:]

pool = AccountPool(ACCOUNTS_CONFIG, ACCOUNTS_STATE)
account = next((a for a in pool.accounts() if a.id == WANTED), None)
if account is None:
    raise SystemExit(f"no account {WANTED!r} in the pool")
print(f"account={account.id}")
access, account_id = account.creds.get()
HEADERS = {
    "Authorization": f"Bearer {access}",
    "chatgpt-account-id": account_id,
    "User-Agent": "codex_cli_rs/0.145.0 (Windows 10.0.20348; x86_64)",
    "originator": "codex_cli_rs",
}
BASE = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"

resp = requests.get(BASE, headers=HEADERS, timeout=30)
print("GET", resp.status_code)
try:
    listing = resp.json()
except ValueError:
    print(resp.text[:2000])
    raise SystemExit(1)
print(json.dumps(listing, indent=2, ensure_ascii=False)[:4000])

if not DO_CONSUME:
    raise SystemExit(0)

credits = listing.get("credits") or listing.get("data") or []
if isinstance(listing, list):
    credits = listing
if not credits:
    raise SystemExit("no credits array found; not consuming")
credit = credits[0]
body = {"credit_id": credit.get("id") or credit.get("credit_id")}
if credit.get("signature"):
    body["signature"] = credit["signature"]
body["redeem_request_id"] = str(uuid.uuid4())
print("POST body:", json.dumps(body))
resp = requests.post(
    BASE + "/consume",
    headers={**HEADERS, "Content-Type": "application/json"},
    json=body,
    timeout=30,
)
print("POST", resp.status_code)
print(resp.text[:4000])
