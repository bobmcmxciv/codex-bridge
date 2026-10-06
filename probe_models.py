import json, sys
import requests
import pool_paths
from codex_accounts import AccountPool

UA = "codex_cli_rs/0.146.0 (Windows 10.0.20348; x86_64)"
pool = AccountPool(pool_paths.ACCOUNTS_CONFIG, pool_paths.ACCOUNTS_STATE)
account = pool.active()
access, account_id = account.creds.get()
print("account:", account.id, file=sys.stderr)

H = {"Authorization": f"Bearer {access}", "chatgpt-account-id": account_id,
     "originator": "codex_cli_rs", "User-Agent": UA, "OpenAI-Beta": "responses=experimental"}

for url in [
    "https://chatgpt.com/backend-api/codex/models",
    "https://chatgpt.com/backend-api/codex/model/list",
    "https://chatgpt.com/backend-api/codex/models/list",
    "https://chatgpt.com/backend-api/models",
]:
    try:
        r = requests.get(url, headers=H, timeout=30)
        body = r.text
        print("\n==", url, "->", r.status_code, "len", len(body))
        if r.status_code == 200:
            try:
                d = r.json()
                s = json.dumps(d, ensure_ascii=False)
                # print any context_window mentions compactly
                print(s[:400])
                import re
                for m in re.finditer(r'"slug":\s*"([^"]+)"[^{]*?', s):
                    pass
                hits = re.findall(r'"(slug|context_window|max_context_window)":\s*("[^"]*"|\d+)', s)
                print("fields:", hits[:60])
            except Exception as e:
                print("parse err", e, body[:200])
        else:
            print(body[:200])
    except Exception as e:
        print(url, "EXC", e)
