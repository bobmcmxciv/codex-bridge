# codex-bridge + cx2cc on vircs

Serves the existing **Codex ChatGPT login** on `vircs` as an **Anthropic-compatible
endpoint** that Claude Code can use, from this machine and from any other host on
the tailnet.

```
Claude Code (vircs / mac / desktop-ht3p09u / desktop-4sqalmg)
        │  ANTHROPIC_BASE_URL=http://<cx2cc-host>:8901
        │  Anthropic  POST /v1/messages   (x-api-key: shared secret)
        ▼
[vircs] cx2cc            <cx2cc-host>:8901     ← Tailscale-only
        │  OpenAI  POST /v1/chat/completions
        ▼
[vircs] codex-bridge     127.0.0.1:8902         ← loopback-only
        │  Responses API + ChatGPT OAuth (auto-refreshing)
        ▼
https://chatgpt.com/backend-api/codex/responses
```

## Why the bridge exists

`cx2cc` translates Anthropic `/v1/messages` → OpenAI `/chat/completions` and expects
an upstream that takes a plain API key. The Codex login on this box is **ChatGPT
OAuth** (`auth_mode: chatgpt`, plan `prolite`) against the **Responses API** — a
different wire format that does not accept an API key. `codex-bridge` is the missing
adapter: it speaks `/chat/completions` to cx2cc and Responses+OAuth upstream.

## Codex logins in use

Several ChatGPT subscriptions are kept side by side. Priority is the array order
in `accounts/pool.json`; the first account that is not parked on a spent quota
serves the request.

| Field | Value |
| --- | --- |
| Pool | `accounts/pool.json` (order = priority), runtime cooldowns in `accounts/state.json` |
| Mode | `chatgpt` (OAuth) — no API key involved |
| Model | `gpt-5.6-sol` (every account) |
| Refresh | automatic per account, via `https://auth.openai.com/oauth/token` |

The bridge refreshes each account's access token ~5 min before expiry and writes
the rotated tokens back to that account's file. One pool entry may point at
`C:\Users\Administrator\.codex\auth.json`, which is the `codex` CLI's own login —
sharing that single file is deliberate, because OpenAI rotates the refresh token
on every refresh and two copies of the same account would invalidate each other.

### Failover

* A `429 {"error":{"type":"usage_limit_reached", "resets_at": ...}}` parks that
  account until its own `resets_at` and **replays the same request on the next
  account**, so the client never sees the switch.
* Other 429s are treated as short-term throttling (60 s park).
* A credential/auth failure parks the account for 10 min.
* `400`/`5xx` are returned as-is — the next account would fail identically.
* Recovery is automatic: once the cooldown lapses, the higher-priority account
  is used again. `GET /usage` also releases an account early if the upstream
  reports the window has reset, and parks one that reports `limit_reached`.
* Cooldowns survive a restart (`accounts/state.json`), and both `pool.json` and
  `state.json` are re-read when they change — no restart needed to reorder
  accounts or clear a cooldown.

### Adding an account

> **`codex login` revokes whatever session is in `~/.codex/auth.json`, at
> OpenAI.** Not a local file change - the old refresh token comes back
> `refresh_token_invalidated` / `token_revoked` forever after, and no backup can
> undo it. Observed 2026-08-01: a request served fine at 10:25, `codex login`
> started 10:26, the same credentials were dead by 10:27.
>
> So a copy of the live credentials is **not** enough protection. `auth.json`
> must be *absent* before starting a login, which is what `detach` is for.

```powershell
cd C:\Users\Administrator\codex-bridge
$py = 'C:\Users\Administrator\cx2cc\.venv\Scripts\python.exe'

& $py manage_accounts.py detach            # CLI login -> pool file, auth.json removed
codex login --device-auth                  # authorise the other account
& $py manage_accounts.py import --id <name> --first   # --first = make it primary
& $py manage_accounts.py list
```

Repeat `detach` + `codex login` + `import` for every further account. To give
the `codex` CLI on this box a working login again, hand one pool account back
to it:

```powershell
& $py manage_accounts.py attach <id>       # pool file -> ~/.codex/auth.json
```

The attached account keeps serving the bridge from that same file - one file
per account, never a copy, because OpenAI rotates the refresh token on each
refresh and a second copy would end up holding a dead one. The cost of
attaching is that the next `codex login` on this box revokes that account;
`detach` first.

`import` refuses API-key stubs and duplicates, and records the account id so a
later re-login into the same file shows up as `identity_mismatch` in
`/accounts` instead of silently changing who is serving.

Other commands: `list`, `order <id> <id> ...`, `park <id> [--hours N]` (force
traffic onto another account without waiting for a 429), `clear <id>` (drop a
cooldown), `remove <id>`, `backup`, `restore` (file-level snapshots; useful
against accidental overwrite, useless against a revocation).


## Files

| Path | Purpose |
| --- | --- |
| `codex_bridge.py` | chat/completions → Codex Responses translation |
| `codex_auth.py` | reads one `auth.json`, refreshes + persists OAuth tokens |
| `codex_accounts.py` | the account pool: priority, cooldowns, failover state |
| `manage_accounts.py` | pool CLI: list / import / backup / restore / order / clear / remove |
| `accounts/pool.json` | ordered account list (priority = array order) |
| `accounts/state.json` | runtime cooldowns, written by the service |
| `accounts/backups/` | credential snapshots taken by `manage_accounts.py backup` |
| `install-services.ps1` | installs both NSSM services (idempotent) |
| `client-setup.sh` / `.ps1` | point a client machine at the endpoint |
| `probe.py` | direct upstream diagnostic, bypasses both proxies |
| `probe_usage.py` | direct `wham/usage` diagnostic, bypasses both proxies |
| `cc-switch-usage-script.js` | paste into CC Switch 用量查询 (custom template) |
| `tests/test_accounts.py` | pool + retry-loop unit tests (`pytest tests/test_accounts.py`) |
| `tests/live_failover.py` | end-to-end failover run against a stub upstream |
| `tests/stub_upstream.py` | fake Codex backend that returns real `usage_limit_reached` 429s |
| `.env` | bridge config incl. the shared secret |
| `logs/` | rotating service logs |

## Services

Both run under NSSM, start automatically at boot, and restart on crash.
`cx2cc` depends on `Tailscale` and `codex-bridge`.

```powershell
Get-Service codex-bridge, cx2cc
Restart-Service codex-bridge
Get-Content C:\Users\Administrator\codex-bridge\logs\codex-bridge.err.log -Tail 40
```

Health:

```powershell
Invoke-RestMethod http://127.0.0.1:8902/health   # bridge (auth + plan)
Invoke-RestMethod http://<cx2cc-host>:8901/health
```

## Usage endpoint

`GET /usage` (alias `/v1/usage`, shared-secret required) returns the subscription's
rate-limit status verbatim from `GET https://chatgpt.com/backend-api/wham/usage` —
the same source the codex CLI's own rate-limit display reads. Responses are cached
for 30 s. cx2cc forwards it, so clients query the front door:

```powershell
Invoke-RestMethod http://<cx2cc-host>:8901/usage -Headers @{ 'x-api-key' = '<shared secret>' }
```

Payload highlights: `plan_type`, `rate_limit.primary_window.used_percent` /
`limit_window_seconds` / `reset_at` (unix), optional `secondary_window`, and
`additional_rate_limits[]` for model-specific lanes. On this `prolite` plan there
is a single 7-day window and `secondary_window` is `null`.

To see it in CC Switch: provider card → 用量查询 (📊) → enable → template「自定义」→
paste `cc-switch-usage-script.js`. The script shows the most-used window as the
gauge (unit %) and lists every window with its reset time in the extra line.

The script is machine-independent because it builds on `{{baseUrl}}`/`{{apiKey}}`
from the provider config: tailnet hosts (mac173, desktops) hit
`http://<cx2cc-host>:8901/usage` directly, txfa608 hits its existing
`http://127.0.0.1:18901` tunnel. The query itself always runs on vircs — client
machines never talk to chatgpt.com and hold no Codex credentials. Copies were
dropped 2026-07-29 at `mac173:~/cc-switch-usage-script.js` and
`txfa608:C:\Users\CHN-Unicom\cc-switch-usage-script.js`.

### HTTPS entry (cc-switch validation)

cc-switch rejects plain-HTTP usage URLs on non-localhost hosts ("非 localhost 必须
使用 HTTPS"; only newer builds exempt custom templates). Since 2026-07-29 vircs
therefore also serves the same endpoint over HTTPS:

```
https://<host>.<tailnet>.ts.net/usage        (tailnet only, Let's Encrypt cert)
  └─ tailscale serve (--bg, persistent) → http://127.0.0.1:8901
       └─ netsh portproxy 127.0.0.1:8901 → <cx2cc-host>:8901   (registry-persistent)
```

The portproxy hop exists because tailscaled's serve proxy cannot hairpin to the
machine's own tailscale IP (observed: empty-body 502, request never reaching
cx2cc); cx2cc itself stays bound to <cx2cc-host> only. Inspect with
`tailscale serve status` and `netsh interface portproxy show v4tov4`.
The mac173 script copy hardcodes this HTTPS URL (leave Base URL empty in the
panel, fill API Key only); the tx copy keeps the localhost tunnel, which the
validator exempts.

## Image generation endpoint

`POST /v1/images/generations` (alias `/images/generations`, bridge token required) serves
gpt-image-2 from the ChatGPT backend. This is the call Codex CLI 0.153.4's built-in image
tool makes (captured 2026-09-06): `POST {CODEX_BASE}/images/generations` with the same
bearer / `chatgpt-account-id` / `originator` headers as `/responses`, body
`{prompt, model, size, quality, background}`, OpenAI-Images-shaped answer
`{"data": [{"b64_json"}], "size", "quality", "usage"}`. The bridge walks the account pool
exactly like `/responses` (401 -> one token refresh, quota 429 -> park + next account) and
serves `n` (max 4) by repeating the call, merging `data` and summing `usage`.

Request: `{"prompt": "...", "size": "1536x1024"|"auto", "quality": "low|medium|high|auto",
"background": "auto|opaque", "n": 1}`. Observed 2026-09-06: explicit sizes are honoured
(`1536x1024` came back `1536x1024`; `1024x1024` came back `1254x1254`), the reported
`quality` was `low` for `auto`, `low` and `medium` requests alike, one image takes 11-15 s
and costs ~160-230 `image_tokens` on the subscription. No `x-codex-image-turn-id` header is
needed. Env: `CODEX_IMAGE_MODEL` (default `gpt-image-2`), `CODEX_IMAGE_TIMEOUT` (180 s).

`POST /v1/images/edits` (alias `/images/edits`) is the reference-image variant, captured from
`codex exec -i file.png`: `POST {CODEX_BASE}/images/edits`, plain JSON with the generations body plus
`images: [{"image_url": "data:image/png;base64,..."}, ...]` (up to 16, order meaningful). The bridge accepts
`images` entries as data URLs, bare base64 (assumed PNG) or `{"image_url": ...}`, or a single `image`. Observed
2026-09-06: one input image ≈ 1521 input `image_tokens`, two inputs 3042; output stays ~229; 13-15 s per call.

cx2cc forwards both as `POST /v1/images/{generations,edits}` on the public entry; the client is the
`gpt-image` Claude Code skill (`~/.claude/skills/gpt-image`) on every fleet machine.

## Client setup

macOS / Linux:

```bash
export ANTHROPIC_BASE_URL="http://<cx2cc-host>:8901"
export ANTHROPIC_API_KEY="<shared-secret>"
unset ANTHROPIC_AUTH_TOKEN
claude
```

Windows:

```powershell
$env:ANTHROPIC_BASE_URL='http://<cx2cc-host>:8901'
$env:ANTHROPIC_API_KEY='<shared-secret>'
Remove-Item Env:ANTHROPIC_AUTH_TOKEN -ErrorAction SilentlyContinue
claude
```

Use `ANTHROPIC_API_KEY`, **not** `ANTHROPIC_AUTH_TOKEN`: cx2cc only reads the
`x-api-key` header, and `ANTHROPIC_AUTH_TOKEN` is sent as `Authorization: Bearer`.

## Security model

- `codex-bridge` binds **127.0.0.1 only**. It is never reachable off-box.
- `cx2cc` binds **<cx2cc-host> only** (the Tailscale address). It is deliberately
  *not* on `0.0.0.0`, because this host also holds the non-private address
  `<public-ip>`.
- Firewall rule `cx2cc (Tailscale inbound 8901)` allows 8901 only from `100.64.0.0/10`.
- The shared secret is enforced by the bridge. `CX2CC_UPSTREAM_API_KEY` is
  intentionally left **unset** in `cx2cc/.env`: cx2cc treats a 401 as "try the next
  key", so configuring a fallback would let a wrong client key silently succeed.
- Rotating the secret: change `CODEX_BRIDGE_TOKEN` in `.env`, `Restart-Service
  codex-bridge`, then update clients.

## Known limitations

- Every requested model maps to `gpt-5.6-sol`. Asking for opus/sonnet/haiku does not
  change the backing model; cx2cc itself also hardcodes its upstream model name.
- `max_tokens`, `temperature`, `top_p`, and `stop` are dropped — the Codex backend
  rejects them (`400 Unsupported parameter: max_output_tokens`).
- Anthropic prompt caching, token counting, and batch endpoints are not implemented.
  Claude Code tolerates their absence.
- Streaming input-token counts come back as `0`; this is a cx2cc behaviour.
- Both processes use the Flask development server. Fine for personal use; it is not
  a hardened multi-tenant server.
- Capacity is whatever the `prolite` ChatGPT subscription allows, shared with the
  `codex` CLI on this machine.
