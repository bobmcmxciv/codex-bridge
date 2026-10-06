import json, sys, requests, pool_paths
from codex_accounts import AccountPool
UA = "codex_cli_rs/0.146.0 (Windows 10.0.20348; x86_64)"
pool = AccountPool(pool_paths.ACCOUNTS_CONFIG, pool_paths.ACCOUNTS_STATE)
account = pool.active(); access, account_id = account.creds.get()
H = {"Authorization": f"Bearer {access}", "chatgpt-account-id": account_id,
     "originator": "codex_cli_rs", "User-Agent": UA, "OpenAI-Beta": "responses=experimental"}
for cv in ["0.146.0", "0.145.0"]:
    url = f"https://chatgpt.com/backend-api/codex/models?client_version={cv}"
    r = requests.get(url, headers=H, timeout=30)
    print("==", url, "->", r.status_code, "len", len(r.text))
    if r.status_code == 200:
        d = r.json()
        print(json.dumps(d, ensure_ascii=False)[:600])
        open("/tmp/codex_models.json","w",encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=2))
        break
    else:
        print(r.text[:300])
