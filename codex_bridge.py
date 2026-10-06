"""
codex-bridge - OpenAI Chat Completions API -> ChatGPT Codex Responses API.

This is the missing upstream for cx2cc. cx2cc speaks Anthropic /v1/messages on
its front side and OpenAI /chat/completions on its back side, but a `codex login`
session is ChatGPT OAuth against the Responses API, which is a different wire
format and cannot accept a plain API key.

    cx2cc :8901  --/chat/completions-->  codex-bridge :8902
                                              |
                                              v
                        https://chatgpt.com/backend-api/codex/responses
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from itertools import chain
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request

import pool_paths
from codex_accounts import AUTH_COOLDOWN, THROTTLE_COOLDOWN, AccountPool

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env", verbose=False)

CODEX_BASE = os.environ.get("CODEX_BASE_URL", "https://chatgpt.com/backend-api/codex").rstrip("/")
CODEX_USAGE_URL = os.environ.get(
    "CODEX_USAGE_URL", "https://chatgpt.com/backend-api/wham/usage"
)
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.6-sol")
# Upstream-real slugs a client may request directly; anything else serves
# CODEX_MODEL. Lets cx2cc pass through e.g. gpt-5.4 for its 1M context lane.
CODEX_MODEL_ALLOWED = {
    m.strip()
    for m in os.environ.get(
        "CODEX_MODEL_ALLOWED", "gpt-5.4,gpt-5.6-sol,gpt-5.6-terra,gpt-5.6-luna"
    ).split(",")
    if m.strip()
}
# Fallback lane for prompts the primary model refuses as over-window. gpt-5.4
# has max_context_window 1,000,000 (measured live 2026-08-09: 903k accepted)
# vs gpt-5.6-sol's hard 272k. Empty disables the fallback.
CODEX_MODEL_LONG = os.environ.get("CODEX_MODEL_LONG", "").strip()
# The catalog endpoint 400s without client_version (probed 2026-08-11), and the
# catalog it returns is *gated by that version*: the same account got no
# gpt-6-astra at client_version=0.146.0 but did at 0.153.4 (probed 2026-09-06).
# A pinned constant therefore silently freezes the model list at whatever the
# pin knew about, so the version is discovered from the installed Codex CLI and
# only pinned when CODEX_CLIENT_VERSION is set explicitly.
_CODEX_CLIENT_VERSION_BASELINE = "0.153.4"
_VERSION_RE = __import__("re").compile(r"(\d+\.\d+\.\d+)")


def _codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME", "").strip()
    return Path(raw) if raw else Path.home() / ".codex"


def _version_from_cli(command: str) -> str | None:
    """`codex --version` prints `codex-cli 0.153.4`; None when it cannot run."""
    import subprocess
    try:
        out = subprocess.run(
            [command, "--version"], capture_output=True, text=True, timeout=8,
            shell=os.name == "nt" and not os.path.isabs(command),
        )
    except Exception:
        return None
    m = _VERSION_RE.search((out.stdout or "") + (out.stderr or ""))
    return m.group(1) if m else None


def _version_from_models_cache() -> str | None:
    """The CLI stamps its own version into ~/.codex/models_cache.json on every
    catalog refresh; the service runs as LocalSystem without the user's PATH,
    so this file is the most reliable witness of the installed CLI."""
    try:
        data = json.loads((_codex_home() / "models_cache.json").read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    m = _VERSION_RE.search(str(data.get("client_version") or ""))
    return m.group(1) if m else None


def _version_from_npm_package() -> str | None:
    home = _codex_home().parent
    for candidate in (
        home / "AppData" / "Roaming" / "npm" / "node_modules" / "@openai" / "codex" / "package.json",
        Path(os.environ.get("APPDATA", "")) / "npm" / "node_modules" / "@openai" / "codex" / "package.json",
    ):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        m = _VERSION_RE.search(str(data.get("version") or ""))
        if m:
            return m.group(1)
    return None


def _detect_codex_client_version() -> tuple[str, str]:
    """(version, source). Explicit env pin wins; otherwise the installed CLI."""
    pinned = os.environ.get("CODEX_CLIENT_VERSION", "").strip()
    if pinned:
        return pinned, "env:CODEX_CLIENT_VERSION"
    cli_path = os.environ.get("CODEX_CLI_PATH", "").strip()
    if cli_path:
        v = _version_from_cli(cli_path)
        if v:
            return v, "cli:CODEX_CLI_PATH"
    v = _version_from_cli("codex")
    if v:
        return v, "cli:PATH"
    v = _version_from_models_cache()
    if v:
        return v, "models_cache.json"
    v = _version_from_npm_package()
    if v:
        return v, "npm:@openai/codex"
    return _CODEX_CLIENT_VERSION_BASELINE, "baseline"


CODEX_CLIENT_VERSION, CODEX_CLIENT_VERSION_SOURCE = _detect_codex_client_version()

# Detection used to run once at import, so `npm i -g @openai/codex` only reached
# the catalog after a service restart (2026-09-30: gpt-6.1-sol stayed hidden
# behind the boot-time 0.156.0). It is now re-run from the catalog path at most
# every _VERSION_RECHECK_INTERVAL; only an upgrade is adopted, so a transient CLI
# failure that falls back to the older models_cache.json stamp cannot roll back.
_VERSION_RECHECK_INTERVAL = 600
_version_state = {"checked_at": time.time()}
_version_lock = threading.Lock()


def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def _recheck_client_version(force: bool = False) -> bool:
    """Adopt a newer installed CLI version; True when the version changed."""
    global CODEX_CLIENT_VERSION, CODEX_CLIENT_VERSION_SOURCE, CODEX_UA
    now = time.time()
    if not force and now - _version_state["checked_at"] < _VERSION_RECHECK_INTERVAL:
        return False
    if not _version_lock.acquire(blocking=False):
        return False
    try:
        _version_state["checked_at"] = now
        version, source = _detect_codex_client_version()
        pinned = source.startswith("env:")
        try:
            newer = _version_tuple(version) > _version_tuple(CODEX_CLIENT_VERSION)
        except ValueError:
            newer = False
        if version == CODEX_CLIENT_VERSION or not (newer or pinned):
            return False
        log.info("codex client version %s -> %s (%s)", CODEX_CLIENT_VERSION, version, source)
        CODEX_CLIENT_VERSION, CODEX_CLIENT_VERSION_SOURCE = version, source
        if not os.environ.get("CODEX_USER_AGENT"):
            CODEX_UA = _default_user_agent(version)
        return True
    finally:
        _version_lock.release()


def _strip_window_suffix(name: str) -> str:
    """Drop a trailing [..] marker (e.g. gpt-5.4[1m]) before allowlist checks.

    Claude Code encodes the context-window override in the model-name suffix,
    so clients asking for the 1M lane send "gpt-5.4[1m]". Without stripping,
    that fails the allowlist and silently serves CODEX_MODEL at 272k while the
    client believes it has 1M (measured 2026-08-11, HANDOFF-models-20260812).
    """
    name = str(name or "").strip()
    if name.endswith("]"):
        i = name.rfind("[")
        if i > 0:
            return name[:i].strip()
    return name


# Upstream model catalog (GET {CODEX_BASE}/models?client_version=...), cached.
# Source of truth for the passthrough allowlist and /v1/models: all nine
# catalog slugs verified serving on 2026-08-11 (probe_model_serve.py).
#
# The cache is keyed by (account, client_version): the catalog is account- and
# version-gated upstream, so a snapshot taken for one account must not be
# served for another after a pool failover, and a version change (explicit
# refresh) must not be masked by a warm cache.
_CATALOG_TTL = 3600
_CATALOG_RETRY_BACKOFF = 120
_catalog_lock = threading.Lock()
_catalog_state: dict = {
    "key": None,        # (account_id, client_version) the snapshot belongs to
    "ts": 0.0,          # time the snapshot was fetched (0 = never)
    "next_try": 0.0,    # earliest time a refetch is attempted
    "models": [],
    "error": None,      # last fetch failure, kept while serving a stale snapshot
}


def _fetch_catalog_once(client_version: str) -> tuple[str, list[dict]]:
    """(account_id, models) from the upstream catalog for the active account."""
    account = pool.active()
    access, account_id = account.creds.get()
    resp = requests.get(
        f"{CODEX_BASE}/models",
        params={"client_version": client_version},
        headers={
            "Authorization": f"Bearer {access}",
            "chatgpt-account-id": account_id,
            "originator": "codex_cli_rs",
            "User-Agent": CODEX_UA,
            "OpenAI-Beta": "responses=experimental",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"catalog HTTP {resp.status_code}")
    return account_id, [m for m in resp.json().get("models", []) if m.get("slug")]


def _catalog_snapshot(force: bool = False) -> dict:
    """Current catalog plus provenance: models, fetched_at, stale, error, key.

    `stale` is True when the snapshot is older than the TTL (a refetch failed
    and the last good copy is being served) or when it was taken for a
    different account/version than the current one."""
    _recheck_client_version(force=force)
    now = time.time()
    try:
        current_account = pool.active().creds.get()[1]
    except Exception:
        current_account = None
    key = (current_account, CODEX_CLIENT_VERSION)
    with _catalog_lock:
        fresh = (
            _catalog_state["models"]
            and _catalog_state["key"] == key
            and now - _catalog_state["ts"] < _CATALOG_TTL
        )
        if fresh and not force:
            return dict(_catalog_state, stale=False)
        if not force and now < _catalog_state["next_try"]:
            return dict(_catalog_state, stale=True)
    try:
        account_id, models = _fetch_catalog_once(CODEX_CLIENT_VERSION)
        if not models:
            raise RuntimeError("catalog returned no models")
        error = None
    except Exception as exc:
        log.warning("model catalog fetch failed (client_version=%s): %s", CODEX_CLIENT_VERSION, exc)
        account_id, models, error = None, [], str(exc)
    with _catalog_lock:
        if models:
            _catalog_state.update(
                key=(account_id, CODEX_CLIENT_VERSION), ts=now, next_try=now,
                models=models, error=None,
            )
            return dict(_catalog_state, stale=False)
        # keep the last snapshot; back off retries a little
        _catalog_state["next_try"] = now + _CATALOG_RETRY_BACKOFF
        _catalog_state["error"] = error
        return dict(_catalog_state, stale=True)


def _model_catalog() -> list[dict]:
    return _catalog_snapshot()["models"]


def _allowed_models() -> set[str]:
    return CODEX_MODEL_ALLOWED | {m["slug"] for m in _model_catalog()}
# The User-Agent advertises the same client version the catalog is asked for;
# an explicit CODEX_USER_AGENT still overrides it wholesale.
def _default_user_agent(version: str) -> str:
    return f"codex_cli_rs/{version} (Windows 10.0.20348; x86_64)"


CODEX_UA = os.environ.get("CODEX_USER_AGENT", _default_user_agent(CODEX_CLIENT_VERSION))
REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "").strip()
BRIDGE_TOKEN = os.environ.get("CODEX_BRIDGE_TOKEN", "").strip()
LISTEN_HOST = os.environ.get("CODEX_BRIDGE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("CODEX_BRIDGE_PORT", "8902"))
ACCOUNTS_CONFIG = pool_paths.ACCOUNTS_CONFIG
ACCOUNTS_STATE = pool_paths.ACCOUNTS_STATE

DEFAULT_INSTRUCTIONS = "You are a helpful coding assistant."

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("codex-bridge")

app = Flask(__name__)
pool = AccountPool(ACCOUNTS_CONFIG, ACCOUNTS_STATE)


# ---------------------------------------------------------------------------
# request translation: chat/completions -> responses
# ---------------------------------------------------------------------------

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _user_content_blocks(content) -> list[dict]:
    """Map an OpenAI user content value into Responses input_* blocks."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    blocks: list[dict] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                if block:
                    blocks.append({"type": "input_text", "text": str(block)})
                continue
            btype = block.get("type")
            if btype == "text":
                blocks.append({"type": "input_text", "text": block.get("text", "")})
            elif btype == "image_url":
                url = (block.get("image_url") or {}).get("url", "")
                if url:
                    blocks.append({"type": "input_image", "image_url": url})
    if not blocks:
        blocks = [{"type": "input_text", "text": _text_of(content)}]
    return blocks


# Hosted web search. Claude Code's WebSearch reaches the bridge (via cx2cc) as
# a chat/completions tool `{"type": "web_search", ...}` spelled like the
# Responses hosted tool. The ChatGPT backend accepts that tool, `tool_choice
# {"type": "web_search"}`, `filters.allowed_domains` and the
# `web_search_call.action.sources` include (all probed 2026-09-28); it answers
# with `web_search_call` output items and `url_citation` annotations.
_WEB_SEARCH_TOOL_FIELDS = ("filters", "user_location", "search_context_size")
_WEB_SEARCH_INCLUDE = "web_search_call.action.sources"


# Reasoning carry-over. With store=false the backend keeps nothing between
# requests, so unless the client sends a turn's reasoning items back, every
# step of a tool loop starts without the reasoning that led to it. Codex CLI
# always asks for `reasoning.encrypted_content` and replays the items; this
# bridge did neither, which is why the same model did markedly worse behind
# Claude Code than under Codex-style clients (2026-10-05 replays: gpt-6-luna
# 9/24 vs 18/24 hidden tests on one task). The finished items travel to the
# chat/completions caller as a `reasoning_items` extension on the assistant
# delta/message and come back on that assistant message in the next request.
# The backend accepts replayed items without their `rs_` id (probed
# 2026-10-05), and the id is dropped so nothing per-request leaks into history.
_REASONING_INCLUDE = "reasoning.encrypted_content"
REASONING_REPLAY = os.environ.get("CODEX_REASONING_REPLAY", "").strip().lower() not in (
    "0", "off", "false", "no",
)


def _reasoning_item_out(item: dict) -> dict | None:
    """A finished upstream reasoning item, reduced to what a replay needs."""
    encrypted = item.get("encrypted_content")
    if not encrypted:
        return None
    return {"encrypted_content": encrypted, "summary": item.get("summary") or []}


def _reasoning_items_in(msg: dict) -> list[dict]:
    """Responses input items for the reasoning carried on an assistant message."""
    out = []
    for item in msg.get("reasoning_items") or []:
        if isinstance(item, dict) and item.get("encrypted_content"):
            out.append({
                "type": "reasoning",
                "encrypted_content": item["encrypted_content"],
                "summary": item.get("summary") or [],
            })
    return out


def _web_search_tool(tool: dict) -> dict:
    """Keep only the fields the Responses web_search tool defines.

    chat/completions' `web_search_options.user_location` nests the fields
    under `approximate`; the Responses tool wants them flat.
    """
    spec: dict = {"type": "web_search"}
    for field in _WEB_SEARCH_TOOL_FIELDS:
        value = tool.get(field)
        if value:
            spec[field] = value
    location = spec.get("user_location")
    if isinstance(location, dict) and isinstance(location.get("approximate"), dict):
        spec["user_location"] = {"type": "approximate", **location["approximate"]}
    return spec


def _web_search_call_out(item: dict) -> dict:
    """A finished upstream web_search_call, as carried in a chat delta/message."""
    return {
        "id": item.get("id", ""),
        "status": item.get("status", ""),
        "action": item.get("action") or {},
    }


def _annotation_out(ann) -> dict | None:
    """Responses url_citation -> chat/completions annotation (its standard shape)."""
    if not isinstance(ann, dict) or ann.get("type") != "url_citation" or not ann.get("url"):
        return None
    return {
        "type": "url_citation",
        "url_citation": {
            "url": ann.get("url", ""),
            "title": ann.get("title", ""),
            "start_index": ann.get("start_index", 0),
            "end_index": ann.get("end_index", 0),
        },
    }


def translate_request(body: dict) -> dict:
    instructions: list[str] = []
    items: list[dict] = []
    in_conversation = False

    for msg in body.get("messages", []):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system" or role == "developer":
            text = _text_of(content)
            if not text.strip():
                continue
            if not in_conversation:
                instructions.append(text)
            else:
                # A system message between turns stays where it is, as a
                # developer message. Folding it into `instructions` rewrote
                # the start of the prompt every time a client (Claude Code's
                # hook context, token budget notes) added one, so the
                # upstream prompt cache only ever covered the part before it.
                items.append(
                    {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": text}],
                    }
                )
            continue

        in_conversation = True

        if role == "tool":
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": _text_of(content),
                }
            )
            continue

        if role == "assistant":
            # Reasoning precedes what it produced, as in the upstream output.
            # A turn with nothing after it would leave a dangling item, so it
            # is replayed only alongside text or tool calls.
            text = _text_of(content)
            if REASONING_REPLAY and (text.strip() or msg.get("tool_calls")):
                items.extend(_reasoning_items_in(msg))
            if text.strip():
                items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id", ""),
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    }
                )
            continue

        # user (and anything unexpected)
        items.append(
            {
                "type": "message",
                "role": "user",
                "content": _user_content_blocks(content),
            }
        )

    requested = _strip_window_suffix(body.get("model"))
    payload: dict = {
        "model": requested if requested in _allowed_models() else CODEX_MODEL,
        "instructions": "\n\n".join(instructions).strip() or DEFAULT_INSTRUCTIONS,
        "input": items,
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": bool(body.get("parallel_tool_calls", False)),
        "store": False,
        "stream": True,
    }

    web_search = False
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "web_search":
            payload["tools"].append(_web_search_tool(tool))
            web_search = True
            continue
        fn = tool.get("function") or {}
        if not fn.get("name"):
            continue
        payload["tools"].append(
            {
                "type": "function",
                "name": fn["name"],
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )

    # chat/completions' own spelling of a search-enabled request.
    options = body.get("web_search_options")
    if isinstance(options, dict) and not web_search:
        payload["tools"].append(_web_search_tool({"type": "web_search", **options}))
        web_search = True

    include = []
    if REASONING_REPLAY:
        include.append(_REASONING_INCLUDE)
    if web_search:
        # Without this the per-search `action.sources` list is absent and only
        # the final text's citations remain.
        include.append(_WEB_SEARCH_INCLUDE)
    if include:
        payload["include"] = include

    choice = body.get("tool_choice")
    if isinstance(choice, str) and choice in ("auto", "none", "required"):
        payload["tool_choice"] = choice
    elif isinstance(choice, dict) and choice.get("type") == "function":
        payload["tool_choice"] = {
            "type": "function",
            "name": (choice.get("function") or {}).get("name", ""),
        }
    elif isinstance(choice, dict) and choice.get("type") == "web_search" and web_search:
        payload["tool_choice"] = {"type": "web_search"}

    # cx2cc >= 0.4 derives a per-conversation prompt_cache_key itself; honour it
    # so the whole chain agrees on one key. _conversation_key stays as the
    # fallback for direct chat/completions clients that send none.
    if body.get("prompt_cache_key"):
        payload["prompt_cache_key"] = str(body["prompt_cache_key"])

    if REASONING_EFFORT:
        payload["reasoning"] = {"effort": REASONING_EFFORT}

    # max_tokens / temperature / top_p / stop are intentionally dropped. The
    # ChatGPT Codex backend rejects them outright, e.g. it answers
    # `400 {"detail":"Unsupported parameter: max_output_tokens"}`. Claude Code
    # always sends max_tokens, so forwarding it would break every request.
    return payload


# ---------------------------------------------------------------------------
# upstream
# ---------------------------------------------------------------------------

def _headers(access: str, account_id: str, session_id: str) -> dict:
    return {
        "Authorization": f"Bearer {access}",
        "chatgpt-account-id": account_id,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "session_id": session_id,
        "User-Agent": CODEX_UA,
    }


def _conversation_key(payload: dict) -> str:
    """Stable id for the conversation a request belongs to.

    The official codex CLI keeps one session_id per conversation; the backend
    uses it and prompt_cache_key for cache-affine routing. Generating a fresh
    uuid per request scattered consecutive turns across cache nodes, which is
    why observed prompt-cache hit rates sat around 13% instead of 90%+. Every
    turn of one Claude Code session repeats the same instructions and first
    user message, so hashing those yields a per-conversation constant.
    """
    first_user = None
    for item in payload.get("input") or []:
        if item.get("type") == "message" and item.get("role") == "user":
            first_user = item
            break
    seed = json.dumps(
        [payload.get("instructions", ""), first_user],
        ensure_ascii=False, sort_keys=True,
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


QUOTA_ERROR_TYPES = {"usage_limit_reached", "usage_limit", "quota_exceeded"}


class UpstreamFailure:
    """Stand-in for a requests.Response once every account has been tried.

    Exposes just the two attributes the route touches on a non-200, so the
    caller does not need to know whether a real response survived.
    """

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def close(self) -> None:  # pragma: no cover - interface parity
        pass


def _quota_signal(text: str) -> tuple[bool, object, object]:
    """Is this 429 a subscription limit (switch accounts) or throttling (wait)?

    Body shape:
        {"error":{"type":"usage_limit_reached","plan_type":"pro",
                  "resets_at":1785912763,"resets_in_seconds":521062}}
    """
    try:
        err = (json.loads(text) or {}).get("error") or {}
    except Exception:
        return ("usage_limit_reached" in text, None, None)
    if not isinstance(err, dict):
        return ("usage_limit_reached" in text, None, None)
    is_quota = str(err.get("type") or "") in QUOTA_ERROR_TYPES
    return is_quota, err.get("resets_at"), err.get("resets_in_seconds")


def call_upstream(payload: dict):
    """POST to the Codex responses endpoint, walking the account pool.

    Per account: one forced token refresh on 401. Across accounts: a
    subscription that reports its limit reached is parked until the window
    resets and the same request is replayed on the next account, so the client
    never sees the switch. Errors that are not account-specific (400, 5xx) are
    returned as-is - the next account would fail identically.

    Returns (response_or_failure, account_that_served_or_None).
    """
    session_id = payload.get("prompt_cache_key") or _conversation_key(payload)
    payload.setdefault("prompt_cache_key", session_id)

    failure: UpstreamFailure | None = None
    tried: list[str] = []

    for account in pool.candidates():
        resp = None
        for attempt in (0, 1):
            try:
                access, account_id = account.creds.get(force_refresh=(attempt == 1))
            except Exception as exc:
                log.error("account %s: credential error: %s", account.id, exc)
                pool.mark_unavailable(account, AUTH_COOLDOWN, "credential_error", str(exc))
                failure = failure or UpstreamFailure(
                    502, f"Codex credential error on {account.id}: {exc}"
                )
                resp = None
                break
            resp = requests.post(
                f"{CODEX_BASE}/responses",
                json=payload,
                headers=_headers(access, account_id, session_id),
                stream=True,
                timeout=600,
            )
            if resp.status_code == 401 and attempt == 0:
                log.warning("account %s: upstream 401, forcing token refresh", account.id)
                resp.close()
                continue
            break

        if resp is None:
            continue

        tried.append(account.id)

        if resp.status_code == 200:
            pool.mark_ok(account)
            resp.encoding = "utf-8"
            return resp, account

        detail = resp.text[:500]
        resp.close()

        if resp.status_code == 429:
            is_quota, resets_at, resets_in = _quota_signal(detail)
            if is_quota:
                pool.mark_quota(account, resets_at, resets_in, detail)
            else:
                pool.mark_unavailable(account, THROTTLE_COOLDOWN, "throttled", detail)
            failure = UpstreamFailure(429, detail)
            continue

        if resp.status_code in (401, 403):
            pool.mark_unavailable(account, AUTH_COOLDOWN, "auth_rejected", detail)
            failure = UpstreamFailure(resp.status_code, detail)
            continue

        return UpstreamFailure(resp.status_code, detail), account

    if failure is None:
        failure = UpstreamFailure(502, "No usable Codex account in the pool")
    log.error("no account could serve the request (tried: %s)", ", ".join(tried) or "none")
    return failure, None


def iter_events(resp):
    for raw in resp.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if payload == "[DONE]":
            return
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


# Events that precede the accept/refuse decision. Anything else means the
# upstream has committed to generating output for this request.
_PREAMBLE_EVENTS = ("response.created", "response.in_progress")
_ERROR_EVENTS = ("error", "response.failed")
# Only guards against a pathological stream that emits preamble forever.
_PEEK_LIMIT = 50


def _peek_stream(events):
    """Read events until the stream proves itself good or bad.

    The Codex backend answers HTTP 200 first and only then streams a refusal
    (e.g. context_length_exceeded at sequence_number 2). Peeking before the
    SSE response is committed lets a refusal come back as a real HTTP error
    instead of an empty-but-successful completion.

    Returns (kind, error_event_or_None, buffered_events); kind is "content",
    "error", or "empty" (stream ended without any output event).
    """
    buffered = []
    for evt in events:
        buffered.append(evt)
        etype = str(evt.get("type", ""))
        if etype in _ERROR_EVENTS:
            return "error", evt, buffered
        if etype not in _PREAMBLE_EVENTS:
            return "content", None, buffered
        if len(buffered) >= _PEEK_LIMIT:
            return "content", None, buffered
    return "empty", None, buffered


def _upstream_error_info(evt) -> tuple[str, str]:
    err = evt.get("error") if evt.get("type") == "error" else None
    if not isinstance(err, dict):
        err = ((evt.get("response") or {}).get("error")) or {}
    code = str(err.get("code") or err.get("type") or "upstream_error")
    msg = str(err.get("message") or json.dumps(evt, ensure_ascii=False)[:300])
    return code, msg


# An in-stream refusal of a request that fits the advertised window is pod
# state, not client fault: the same payload under a fresh prompt_cache_key /
# session_id routinely lands on a node that accepts it. The request is
# replayed once under a fresh key, and the mapping is remembered so the
# conversation keeps hitting the replacement node's prompt cache instead of
# bouncing between refusal and re-roll on every turn.
_REBIND_TTL = 7 * 24 * 3600
_rebind_lock = threading.Lock()
_rebind: dict[str, tuple[str, float]] = {}


def _rebind_get(key: str):
    with _rebind_lock:
        entry = _rebind.get(key)
        if not entry:
            return None
        if time.time() - entry[1] > _REBIND_TTL:
            _rebind.pop(key, None)
            return None
        return entry[0]


def _rebind_set(key: str) -> str:
    fresh = f"rebind-{uuid.uuid4()}"
    with _rebind_lock:
        if len(_rebind) > 4096:
            now = time.time()
            for stale in [k for k, v in _rebind.items() if now - v[1] > _REBIND_TTL]:
                _rebind.pop(stale, None)
            if len(_rebind) > 4096:
                _rebind.clear()
        _rebind[key] = (fresh, time.time())
    return fresh


def _rebind_drop(key: str) -> None:
    with _rebind_lock:
        _rebind.pop(key, None)


# ---------------------------------------------------------------------------
# response translation: responses -> chat/completions
# ---------------------------------------------------------------------------

def _usage_out(usage: dict) -> dict:
    """Codex Responses usage -> OpenAI chat/completions usage.

    Carries the cached-token detail through as well, so downstream can report
    cache reads instead of assuming zero.
    """
    details = usage.get("input_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("input_tokens", 0),
        "completion_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "prompt_tokens_details": {"cached_tokens": details.get("cached_tokens", 0)},
    }


def _chunk(cid: str, created: int, model: str, delta: dict, finish=None) -> str:
    obj = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def stream_translate(events, model: str):
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    tool_slots: dict[str, int] = {}
    finish = "stop"
    role_sent = False

    for evt in events:
        etype = evt.get("type", "")

        if not role_sent:
            yield _chunk(cid, created, model, {"role": "assistant", "content": ""})
            role_sent = True

        if etype == "response.output_text.delta":
            yield _chunk(cid, created, model, {"content": evt.get("delta", "")})

        elif etype == "response.output_item.added":
            item = evt.get("item") or {}
            if item.get("type") == "function_call":
                # Slots are keyed by the *item* id (`fc_...`), because that is what
                # the argument-delta events reference via `item_id`. The `call_id`
                # (`call_...`) is a different value and only travels on this event,
                # so it has to be emitted here or it is lost.
                item_id = item.get("id", "")
                call_id = item.get("call_id") or item_id
                idx = len(tool_slots)
                tool_slots[item_id] = idx
                yield _chunk(
                    cid, created, model,
                    {
                        "tool_calls": [
                            {
                                "index": idx,
                                "id": call_id,
                                "type": "function",
                                "function": {"name": item.get("name", ""), "arguments": ""},
                            }
                        ]
                    },
                )

        elif etype == "response.function_call_arguments.delta":
            item_id = evt.get("item_id", "")
            idx = tool_slots.get(item_id)
            if idx is None:
                # Never fall back to `output_index`: it counts *all* output items
                # (reasoning, text, ...), so it drifts from the tool slot index and
                # would open a phantom tool block with an empty id downstream.
                idx = len(tool_slots)
                tool_slots[item_id] = idx
                yield _chunk(
                    cid, created, model,
                    {
                        "tool_calls": [
                            {
                                "index": idx,
                                "id": item_id or f"call_{uuid.uuid4().hex[:24]}",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        ]
                    },
                )
            yield _chunk(
                cid, created, model,
                {"tool_calls": [{"index": idx, "function": {"arguments": evt.get("delta", "")}}]},
            )

        elif etype == "response.output_item.done":
            item = evt.get("item") or {}
            if item.get("type") == "function_call":
                finish = "tool_calls"
            elif item.get("type") == "reasoning":
                out = _reasoning_item_out(item)
                if out:
                    yield _chunk(cid, created, model, {"reasoning_items": [out]})
            elif item.get("type") == "web_search_call":
                # Reported once finished, when its `action` (query, sources)
                # is known; the `.added` event carries only the id.
                yield _chunk(cid, created, model, {"web_search_calls": [_web_search_call_out(item)]})

        elif etype == "response.output_text.annotation.added":
            ann = _annotation_out(evt.get("annotation"))
            if ann:
                yield _chunk(cid, created, model, {"annotations": [ann]})

        elif etype in ("response.failed", "error"):
            log.error("upstream stream error: %s", json.dumps(evt)[:400])
            finish = "stop"

        elif etype == "response.completed":
            usage = (evt.get("response") or {}).get("usage") or {}
            obj = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": _usage_out(usage),
            }
            yield f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
            return

    yield _chunk(cid, created, model, {}, finish=finish)
    yield "data: [DONE]\n\n"


def collect_nonstream(events, model: str) -> dict:
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    web_search_calls: list[dict] = []
    annotations: list[dict] = []
    reasoning_items: list[dict] = []
    usage = {}
    finish = "stop"

    for evt in events:
        etype = evt.get("type", "")
        if etype == "response.output_text.delta":
            text_parts.append(evt.get("delta", ""))
        elif etype == "response.output_item.done":
            item = evt.get("item") or {}
            if item.get("type") == "function_call":
                tool_calls.append(
                    {
                        "id": item.get("call_id", ""),
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", "{}"),
                        },
                    }
                )
                finish = "tool_calls"
            elif item.get("type") == "reasoning":
                out = _reasoning_item_out(item)
                if out:
                    reasoning_items.append(out)
            elif item.get("type") == "web_search_call":
                web_search_calls.append(_web_search_call_out(item))
            elif item.get("type") == "message":
                # The finished message carries every citation; the streamed
                # annotation events are not consumed on this path.
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    for ann in part.get("annotations") or []:
                        out = _annotation_out(ann)
                        if out:
                            annotations.append(out)
        elif etype == "response.completed":
            usage = (evt.get("response") or {}).get("usage") or {}

    message: dict = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if web_search_calls:
        message["web_search_calls"] = web_search_calls
    if annotations:
        message["annotations"] = annotations
    if reasoning_items:
        message["reasoning_items"] = reasoning_items

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
    }


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

def _authorized() -> bool:
    if not BRIDGE_TOKEN:
        return True
    header = request.headers.get("Authorization", "")
    presented = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not presented:
        presented = request.headers.get("x-api-key", "").strip()
    return presented == BRIDGE_TOKEN


def _err(code: int, msg: str):
    return jsonify({"error": {"message": msg, "type": "bridge_error", "code": code}}), code


@app.route("/v1/chat/completions", methods=["POST"])
@app.route("/chat/completions", methods=["POST"])
def chat_completions():
    if not _authorized():
        return _err(401, "Invalid bridge token")

    try:
        body = request.get_json(force=True)
    except Exception as exc:
        return _err(400, f"Invalid JSON: {exc}")

    requested_model = body.get("model") or "(unset)"
    want_stream = bool(body.get("stream", False))

    try:
        payload = translate_request(body)
    except Exception:
        log.exception("request translation failed")
        return _err(400, "Request translation error")

    # Report the model that actually serves the request, not the one the caller
    # asked for. Echoing the request would let a `claude-opus-*` name propagate
    # back through cx2cc and misrepresent what really ran. payload["model"] is
    # always an upstream-real slug (allowlisted request or CODEX_MODEL), and it
    # may switch to CODEX_MODEL_LONG below if the primary refuses the prompt
    # as over-window.
    display_model = payload["model"]

    # Log the first 8 chars of the cache key, not the key itself: enough to spot a
    # key that changes mid-conversation (which silently destroys cache affinity),
    # without writing a full conversation-identifying value to disk.
    key = body.get("prompt_cache_key")
    log.info(
        "-> requested=%s served=%s stream=%s msgs=%s tools=%s cache_key=%s:%s",
        requested_model, payload["model"], want_stream,
        len(body.get("messages", [])), len(payload["tools"]),
        "client" if key else "derived",
        (key or _conversation_key(payload))[:8],
    )

    # Sticky affinity rebind: a conversation whose original key was refused
    # upstream keeps using its replacement key, so its prompt cache lives on.
    orig_key = str(payload.get("prompt_cache_key") or _conversation_key(payload))
    payload.setdefault("prompt_cache_key", orig_key)
    rebound = _rebind_get(orig_key)
    if rebound:
        payload["prompt_cache_key"] = rebound

    n_msgs = len(body.get("messages", []))
    last_code, last_msg = "upstream_error", "unknown upstream failure"

    # Attempt ladder for in-stream refusals of an over-window prompt:
    #   0: as requested   1: fresh affinity key   2: CODEX_MODEL_LONG lane
    for attempt in (0, 1, 2):
        try:
            resp, account = call_upstream(payload)
        except requests.RequestException as exc:
            log.error("upstream connection failed: %s", exc)
            return _err(502, f"Upstream connection failed: {type(exc).__name__}")

        if resp.status_code != 200:
            detail = resp.text[:500]
            log.error("upstream HTTP %s: %s", resp.status_code, detail)
            return _err(resp.status_code, f"Upstream error {resp.status_code}: {detail}")

        events = iter_events(resp)
        kind, err_evt, buffered = _peek_stream(events)

        if kind == "content":
            log.info("<- served by account=%s model=%s key=%s attempt=%s",
                     account.id if account else "?", payload["model"],
                     str(payload["prompt_cache_key"])[:8], attempt)
            display_model = payload["model"]
            live = chain(buffered, events)
            if want_stream:
                return Response(
                    stream_translate(live, display_model),
                    mimetype="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "X-Accel-Buffering": "no",
                        "Connection": "keep-alive",
                    },
                )
            try:
                return jsonify(collect_nonstream(live, display_model))
            except Exception:
                log.exception("response translation failed")
                return _err(500, "Response translation error")

        resp.close()
        if kind == "empty":
            last_code, last_msg = "empty_stream", "upstream stream ended before any output event"
        else:
            last_code, last_msg = _upstream_error_info(err_evt)
        log.error("upstream refused before output (attempt %s, model=%s, key=%s, msgs=%s, code=%s): %s",
                  attempt, payload["model"], str(payload["prompt_cache_key"])[:8],
                  n_msgs, last_code, last_msg[:200])

        if last_code != "context_length_exceeded":
            break
        if attempt == 0:
            payload["prompt_cache_key"] = _rebind_set(orig_key)
            log.warning("re-rolling cache affinity %s -> %s and retrying",
                        orig_key[:8], payload["prompt_cache_key"][:8])
            continue
        if attempt == 1 and CODEX_MODEL_LONG and payload["model"] != CODEX_MODEL_LONG:
            _rebind_drop(orig_key)
            payload["model"] = CODEX_MODEL_LONG
            payload["prompt_cache_key"] = orig_key
            log.warning("falling back to long-context model %s for this prompt",
                        CODEX_MODEL_LONG)
            continue
        break

    if last_code == "context_length_exceeded":
        # Both the pinned node and a fresh one refused; forget the rebind so
        # the next turn rolls a new node instead of reusing a known-bad one.
        _rebind_drop(orig_key)
        status = 400
    else:
        status = 502
    return jsonify({
        "error": {
            "message": f"Upstream error: {last_msg}",
            "type": "upstream_error",
            "code": last_code,
        }
    }), status


@app.route("/v1/responses", methods=["POST"])
@app.route("/responses", methods=["POST"])
def responses_passthrough():
    """Native Responses API entry, for clients that speak Responses directly.

    Chat-completions callers (cx2cc, aider, etc.) still land on
    /v1/chat/completions and get their body translated. Codex CLI 0.135+ removed
    `wire_api = "chat"` and only issues /v1/responses, so this endpoint accepts
    that shape unchanged, walks the same account pool + rebind ladder, and
    streams the ChatGPT backend's SSE bytes back verbatim. `translate_request`
    is skipped: the caller already sent Responses.
    """
    if not _authorized():
        return _err(401, "Invalid bridge token")

    try:
        payload = request.get_json(force=True)
    except Exception as exc:
        return _err(400, f"Invalid JSON: {exc}")

    if not isinstance(payload, dict):
        return _err(400, "Request body must be a JSON object")

    requested_model = payload.get("model") or "(unset)"
    resolved = _strip_window_suffix(requested_model)
    payload["model"] = resolved if resolved in _allowed_models() else CODEX_MODEL

    # The upstream is SSE-only. Chat-completions callers can pretend they got a
    # single JSON object because the bridge aggregates for them; for /v1/responses
    # we would need a full Responses-shape non-stream aggregator to do the same
    # honestly, and codex-cli (the only known caller so far) always sets
    # stream=true. Fail loud rather than serve SSE-in-body under a stream=false
    # request.
    if payload.get("stream") is False:
        return _err(400, "/v1/responses currently requires stream=true")
    payload["stream"] = True

    orig_key = str(payload.get("prompt_cache_key") or _conversation_key(payload))
    payload["prompt_cache_key"] = orig_key
    rebound = _rebind_get(orig_key)
    if rebound:
        payload["prompt_cache_key"] = rebound

    log.info(
        "-> [responses] requested=%s served=%s stream=%s cache_key=%s:%s",
        requested_model,
        payload["model"],
        True,
        "client" if payload.get("prompt_cache_key") == orig_key and not rebound else "rebound",
        str(payload["prompt_cache_key"])[:8],
    )

    last_code, last_msg = "upstream_error", "unknown upstream failure"

    # Same attempt ladder as chat_completions(): a context_length_exceeded gets
    # one retry on a fresh affinity key, then one on CODEX_MODEL_LONG. Every
    # other refusal is returned as-is.
    for attempt in (0, 1, 2):
        try:
            resp, account = call_upstream(payload)
        except requests.RequestException as exc:
            log.error("[responses] upstream connection failed: %s", exc)
            return _err(502, f"Upstream connection failed: {type(exc).__name__}")

        if resp.status_code != 200:
            detail = resp.text[:500]
            log.error("[responses] upstream HTTP %s: %s", resp.status_code, detail)
            return _err(resp.status_code, f"Upstream error {resp.status_code}: {detail}")

        events = iter_events(resp)
        kind, err_evt, buffered = _peek_stream(events)

        if kind == "content":
            log.info(
                "<- [responses] served by account=%s model=%s key=%s attempt=%s",
                account.id if account else "?",
                payload["model"],
                str(payload["prompt_cache_key"])[:8],
                attempt,
            )
            live = chain(buffered, events)

            def generate():
                # Re-emit the peeked events plus everything still to come. The
                # ChatGPT backend sends real Responses SSE, so we just re-serialize
                # each event dict on its own SSE frame — no shape translation.
                try:
                    for evt in live:
                        yield "data: " + json.dumps(evt, ensure_ascii=False) + "\n\n"
                    yield "data: [DONE]\n\n"
                except Exception:
                    log.exception("[responses] stream proxy error")

            return Response(
                generate(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        resp.close()
        if kind == "empty":
            last_code, last_msg = "empty_stream", "upstream stream ended before any output event"
        else:
            last_code, last_msg = _upstream_error_info(err_evt)
        log.error(
            "[responses] upstream refused before output (attempt %s, model=%s, key=%s, code=%s): %s",
            attempt,
            payload["model"],
            str(payload["prompt_cache_key"])[:8],
            last_code,
            last_msg[:200],
        )

        if last_code != "context_length_exceeded":
            break
        if attempt == 0:
            payload["prompt_cache_key"] = _rebind_set(orig_key)
            log.warning(
                "[responses] re-rolling cache affinity %s -> %s and retrying",
                orig_key[:8],
                payload["prompt_cache_key"][:8],
            )
            continue
        if attempt == 1 and CODEX_MODEL_LONG and payload["model"] != CODEX_MODEL_LONG:
            _rebind_drop(orig_key)
            payload["model"] = CODEX_MODEL_LONG
            payload["prompt_cache_key"] = orig_key
            log.warning("[responses] falling back to long-context model %s", CODEX_MODEL_LONG)
            continue
        break

    if last_code == "context_length_exceeded":
        _rebind_drop(orig_key)
        status = 400
    else:
        status = 502
    return jsonify({
        "error": {
            "message": f"Upstream error: {last_msg}",
            "type": "upstream_error",
            "code": last_code,
        }
    }), status


def _reasoning_efforts(m: dict) -> list[str] | None:
    levels = m.get("supported_reasoning_levels")
    if not isinstance(levels, list):
        return None
    efforts = [str(lv.get("effort")) for lv in levels if isinstance(lv, dict) and lv.get("effort")]
    return efforts or None


@app.route("/v1/models", methods=["GET"])
@app.route("/models", methods=["GET"])
def models():
    # Advertise the live upstream catalog (default model first, hidden slugs
    # excluded — they still pass the allowlist if requested explicitly).
    # Falls back to the static allowlist when the catalog is unreachable.
    # `?refresh=1` bypasses the TTL so a client can pull a new catalog on
    # demand (e.g. right after a Codex CLI upgrade changed the gate).
    force = request.args.get("refresh", "").strip().lower() in ("1", "true", "yes")
    snapshot = _catalog_snapshot(force=force)
    catalog = snapshot["models"]
    entries: list[dict] = []
    seen: set[str] = set()
    listed = [m for m in catalog if m.get("visibility") != "hide"]
    listed.sort(key=lambda m: (m["slug"] != CODEX_MODEL, m["slug"]))
    for m in listed:
        entries.append(
            {
                "id": m["slug"],
                "object": "model",
                "created": 1,
                "owned_by": "codex-chatgpt",
                "display_name": m.get("display_name"),
                "context_window": m.get("context_window"),
                "max_context_window": m.get("max_context_window"),
                "reasoning_efforts": _reasoning_efforts(m),
                "is_default": m["slug"] == CODEX_MODEL,
                "source": "catalog",
            }
        )
        seen.add(m["slug"])
    # Config-only entries (CODEX_MODEL / CODEX_MODEL_ALLOWED slugs the catalog
    # did not list) are still advertised so a pinned default keeps working
    # while the catalog is unreachable, but they are marked as such: nothing
    # upstream vouched for them, so no capability metadata is invented.
    for m in [CODEX_MODEL] + sorted(CODEX_MODEL_ALLOWED):
        if m not in seen:
            entries.append({
                "id": m, "object": "model", "created": 1, "owned_by": "codex-chatgpt",
                "is_default": m == CODEX_MODEL, "source": "config",
            })
            seen.add(m)
    return jsonify({
        "object": "list",
        "data": entries,
        "default_model": CODEX_MODEL,
        "client_version": CODEX_CLIENT_VERSION,
        "client_version_source": CODEX_CLIENT_VERSION_SOURCE,
        "catalog_fetched_at": snapshot["ts"] or None,
        "catalog_stale": bool(snapshot["stale"]),
        "catalog_error": snapshot.get("error"),
    })


# The wham/usage payload only changes when requests are consumed, and cc-switch
# may poll or fire repeated "test script" calls; a short cache keeps that from
# hammering the ChatGPT backend. Keyed by account id, since /accounts asks for
# every account in the pool.
USAGE_CACHE_TTL = 30
_usage_cache_lock = threading.Lock()
_usage_cache: dict[str, dict] = {}


def _usage_headers(access: str, account_id: str) -> dict:
    return {
        "Authorization": f"Bearer {access}",
        "chatgpt-account-id": account_id,
        "originator": "codex_cli_rs",
        "User-Agent": CODEX_UA,
    }


def _fetch_usage(account, allow_cache: bool = True):
    """(status, body) of wham/usage for one account; body is dict or str.

    A successful payload is fed back into the pool, so an exhausted or recovered
    subscription is picked up here too, not only when a request burns a 429.
    """
    if allow_cache:
        with _usage_cache_lock:
            entry = _usage_cache.get(account.id)
            if entry and time.time() - entry["at"] < USAGE_CACHE_TTL:
                return 200, entry["body"]

    resp = None
    for attempt in (0, 1):
        try:
            access, account_id = account.creds.get(force_refresh=(attempt == 1))
        except Exception as exc:
            log.error("usage: account %s credential error: %s", account.id, exc)
            return 502, f"Codex credential error: {exc}"
        try:
            resp = requests.get(
                CODEX_USAGE_URL,
                headers=_usage_headers(access, account_id),
                timeout=30,
            )
        except requests.RequestException as exc:
            log.error("usage: account %s connection failed: %s", account.id, exc)
            return 502, f"Upstream connection failed: {type(exc).__name__}"
        if resp.status_code == 401 and attempt == 0:
            log.warning("usage: account %s got 401, forcing token refresh", account.id)
            continue
        break

    if resp.status_code != 200:
        log.error("usage: account %s upstream HTTP %s: %s",
                  account.id, resp.status_code, resp.text[:300])
        return resp.status_code, f"Upstream usage error {resp.status_code}"

    try:
        body = resp.json()
    except ValueError:
        return 502, "Upstream usage response was not valid JSON"

    pool.note_usage(account, body)
    with _usage_cache_lock:
        _usage_cache[account.id] = {"at": time.time(), "body": body}
    return 200, body


@app.route("/v1/usage", methods=["GET"])
@app.route("/usage", methods=["GET"])
def usage():
    """Subscription rate-limit status, straight from the ChatGPT Codex backend.

    Returns the `GET backend-api/wham/usage` payload of the account that would
    serve the next request, verbatim: `plan_type`, `rate_limit.primary_window` /
    `secondary_window` (used_percent, limit_window_seconds, reset_at, ...),
    `additional_rate_limits`, `credits`. This is the same source the codex CLI's
    own rate-limit display reads. `?account=<id>` picks a specific pool member.
    """
    if not _authorized():
        return _err(401, "Invalid bridge token")

    wanted = (request.args.get("account") or "").strip()
    if wanted:
        account = next((a for a in pool.accounts() if a.id == wanted), None)
        if account is None:
            return _err(404, f"No account {wanted!r} in the pool")
    else:
        account = pool.active()

    status, body = _fetch_usage(account)
    if status != 200:
        return _err(status, body if isinstance(body, str) else "Upstream usage error")
    return jsonify(body)


@app.route("/accounts", methods=["GET"])
@app.route("/v1/accounts", methods=["GET"])
def accounts():
    """Pool view: order, identity, cooldowns, and (by default) live usage.

    `?usage=0` skips the upstream calls and reports local state only.
    """
    if not _authorized():
        return _err(401, "Invalid bridge token")

    want_usage = request.args.get("usage", "1") != "0"
    active = pool.active()
    out = []
    for account in pool.accounts():
        info = pool.describe_one(account)
        info["active"] = account.id == active.id
        if want_usage:
            status, body = _fetch_usage(account)
            if status == 200 and isinstance(body, dict):
                rate = (body.get("rate_limit") or {}).get("primary_window") or {}
                info["used_percent"] = rate.get("used_percent")
                info["window_reset_at"] = rate.get("reset_at")
                if rate.get("reset_at"):
                    info["window_reset_local"] = time.strftime(
                        "%Y-%m-%d %H:%M:%S", time.localtime(rate["reset_at"])
                    )
                info["limit_reached"] = (body.get("rate_limit") or {}).get("limit_reached")
                info["plan"] = body.get("plan_type", info.get("plan"))
                info["email"] = body.get("email") or info.get("email")
            else:
                info["usage_error"] = body if isinstance(body, str) else str(status)
        # Re-check after the usage fetch: it may have parked or released this
        # account through pool.note_usage().
        info.update({k: v for k, v in pool.describe_one(account).items()
                     if k.startswith("cooldown") or k == "available"})
        out.append(info)
    return jsonify({"active": active.id, "accounts": out})


# ---------------------------------------------------------------------------
# images: gpt-image-2 through the Codex backend
# ---------------------------------------------------------------------------
#
# Captured from Codex CLI 0.153.4 on 2026-09-06 (logging proxy on the
# openai_base_url): the CLI's built-in image tool is not a server-side
# Responses tool. The model emits an `image_gen__imagegen` call and the CLI
# itself does `POST {CODEX_BASE}/images/generations` with the same ChatGPT
# bearer / chatgpt-account-id / originator headers as /responses and the body
# {prompt, model, size, quality, background}. The answer is OpenAI Images
# shaped: {"created", "data": [{"b64_json"}], "output_format", "quality",
# "size", "usage"}, ~15 s per image, billed to the subscription. The upstream
# body carries no `n`; a multi-image request is served by repeating the call.

IMAGE_MODEL = os.environ.get("CODEX_IMAGE_MODEL", "gpt-image-2")
IMAGE_TIMEOUT = int(os.environ.get("CODEX_IMAGE_TIMEOUT", "180"))
IMAGE_MAX_N = 4
# GPT Image models take up to 16 reference images (Codex imagegen docs); the
# edits endpoint carries them as data URLs in `images`, plain JSON, no multipart.
IMAGE_MAX_INPUTS = 16


def _image_headers(access: str, account_id: str) -> dict:
    return {
        "Authorization": f"Bearer {access}",
        "chatgpt-account-id": account_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "originator": "codex_cli_rs",
        "version": CODEX_CLIENT_VERSION,
        "User-Agent": CODEX_UA,
    }


def _post_upstream_json(path: str, body: dict, timeout: int):
    """POST a plain JSON call to `{CODEX_BASE}/{path}`, walking the account pool.

    Same ladder as call_upstream() minus the streaming: one forced token
    refresh on 401 per account, a quota 429 parks the account and the request
    moves on to the next one, 400/5xx come back as-is. Returns
    (response_or_failure, account_that_served_or_None).
    """
    failure: UpstreamFailure | None = None
    tried: list[str] = []

    for account in pool.candidates():
        resp = None
        for attempt in (0, 1):
            try:
                access, account_id = account.creds.get(force_refresh=(attempt == 1))
            except Exception as exc:
                log.error("account %s: credential error: %s", account.id, exc)
                pool.mark_unavailable(account, AUTH_COOLDOWN, "credential_error", str(exc))
                failure = failure or UpstreamFailure(
                    502, f"Codex credential error on {account.id}: {exc}"
                )
                resp = None
                break
            resp = requests.post(
                f"{CODEX_BASE}/{path}",
                json=body,
                headers=_image_headers(access, account_id),
                timeout=timeout,
            )
            if resp.status_code == 401 and attempt == 0:
                log.warning("account %s: upstream 401 on %s, forcing token refresh", account.id, path)
                resp.close()
                continue
            break

        if resp is None:
            continue

        tried.append(account.id)

        if resp.status_code == 200:
            pool.mark_ok(account)
            return resp, account

        detail = resp.text[:500]
        resp.close()

        if resp.status_code == 429:
            is_quota, resets_at, resets_in = _quota_signal(detail)
            if is_quota:
                pool.mark_quota(account, resets_at, resets_in, detail)
            else:
                pool.mark_unavailable(account, THROTTLE_COOLDOWN, "throttled", detail)
            failure = UpstreamFailure(429, detail)
            continue

        if resp.status_code in (401, 403):
            pool.mark_unavailable(account, AUTH_COOLDOWN, "auth_rejected", detail)
            failure = UpstreamFailure(resp.status_code, detail)
            continue

        return UpstreamFailure(resp.status_code, detail), account

    if failure is None:
        failure = UpstreamFailure(502, "No usable Codex account in the pool")
    log.error("no account could serve %s (tried: %s)", path, ", ".join(tried) or "none")
    return failure, None


def _merge_usage(total: dict, part: dict) -> dict:
    """Sum nested integer usage counters (input_tokens, *_details.image_tokens, ...)."""
    for key, value in (part or {}).items():
        if isinstance(value, dict):
            total[key] = _merge_usage(total.get(key) or {}, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
    return total


def _image_request_common(payload: dict):
    """Validate the fields shared by generations and edits.

    Returns (body, n) or an error string. `size`, `quality` and `background`
    default to "auto" exactly like the Codex CLI; `model` is pinned to
    CODEX_IMAGE_MODEL.
    """
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        return "prompt is required"
    try:
        n = int(payload.get("n") or 1)
    except (TypeError, ValueError):
        return "n must be an integer"
    if not 1 <= n <= IMAGE_MAX_N:
        return f"n must be between 1 and {IMAGE_MAX_N}"
    body = {
        "prompt": prompt,
        "model": IMAGE_MODEL,
        "size": str(payload.get("size") or "auto"),
        "quality": str(payload.get("quality") or "auto"),
        "background": str(payload.get("background") or "auto"),
    }
    return body, n


def _normalize_input_images(payload: dict):
    """`images` (list of data URLs / bare base64 / {"image_url": ...}) or a
    single `image`, into the upstream shape [{"image_url": "data:..."}].
    Returns the list or an error string."""
    raw = payload.get("images")
    if raw is None and payload.get("image"):
        raw = [payload["image"]]
    if not isinstance(raw, list) or not raw:
        return "images must be a non-empty list (data URLs, base64, or {\"image_url\": ...})"
    if len(raw) > IMAGE_MAX_INPUTS:
        return f"at most {IMAGE_MAX_INPUTS} input images"
    out = []
    for i, item in enumerate(raw):
        url = item.get("image_url") if isinstance(item, dict) else item
        if not isinstance(url, str) or not url.strip():
            return f"images[{i}] must be a data URL string or {{\"image_url\": ...}}"
        url = url.strip()
        if not url.startswith("data:"):
            url = "data:image/png;base64," + url  # bare base64: assume PNG
        out.append({"image_url": url})
    return out


def _run_image_request(path: str, body: dict, n: int):
    """POST `body` to `{CODEX_BASE}/{path}` n times and merge the Images JSON."""
    label = f"[{path}]"
    out: dict = {"data": [], "model": IMAGE_MODEL, "usage": {}}
    served = None
    started = time.time()
    for _ in range(n):
        try:
            resp, account = _post_upstream_json(path, body, IMAGE_TIMEOUT)
        except requests.RequestException as exc:
            log.error("%s upstream connection failed: %s", label, exc)
            return _err(502, f"Upstream connection failed: {type(exc).__name__}")

        if resp.status_code != 200:
            detail = resp.text[:500]
            resp.close()
            log.error("%s upstream HTTP %s: %s", label, resp.status_code, detail)
            return _err(resp.status_code, f"Upstream error {resp.status_code}: {detail}")

        try:
            piece = resp.json()
        except ValueError:
            resp.close()
            return _err(502, "Upstream images response was not valid JSON")
        resp.close()

        served = account
        out["data"].extend(piece.get("data") or [])
        for key in ("created", "background", "output_format", "quality", "size"):
            if key in piece:
                out[key] = piece[key]
        out["usage"] = _merge_usage(out["usage"], piece.get("usage") or {})

    usage = out["usage"]
    log.info(
        "<- %s account=%s images=%s size=%s quality=%s in_image_tokens=%s out_image_tokens=%s %.1fs",
        label,
        served.id if served else "?",
        len(out["data"]),
        out.get("size"),
        out.get("quality"),
        (usage.get("input_tokens_details") or {}).get("image_tokens"),
        (usage.get("output_tokens_details") or {}).get("image_tokens"),
        time.time() - started,
    )
    return jsonify(out)


def _read_image_payload():
    """Shared auth + JSON parsing for the image routes. Returns (payload, None) or (None, response)."""
    if not _authorized():
        return None, _err(401, "Invalid bridge token")
    try:
        payload = request.get_json(force=True)
    except Exception as exc:
        return None, _err(400, f"Invalid JSON: {exc}")
    if not isinstance(payload, dict):
        return None, _err(400, "Request body must be a JSON object")
    return payload, None


@app.route("/v1/images/generations", methods=["POST"])
@app.route("/images/generations", methods=["POST"])
def images_generations():
    """gpt-image-2 via the Codex backend, in OpenAI Images API shape.

    Accepts {prompt, size?, quality?, background?, n?}. Answers the upstream
    JSON merged over `n` calls: {"created", "data": [{"b64_json"}, ...],
    "output_format", "quality", "size", "background", "model", "usage"}.
    """
    payload, error = _read_image_payload()
    if error is not None:
        return error
    common = _image_request_common(payload)
    if isinstance(common, str):
        return _err(400, common)
    body, n = common
    log.info(
        "-> [images/generations] n=%s size=%s quality=%s prompt=%r",
        n, body["size"], body["quality"], body["prompt"][:80],
    )
    return _run_image_request("images/generations", body, n)


@app.route("/v1/images/edits", methods=["POST"])
@app.route("/images/edits", methods=["POST"])
def images_edits():
    """Image-to-image: reference / edit inputs plus a prompt, same answer shape.

    Captured from Codex CLI 0.153.4 (`codex exec -i file.png`, 2026-09-06):
    `POST {CODEX_BASE}/images/edits` with the generations body plus
    `images: [{"image_url": "data:image/png;base64,..."}, ...]`. Order is
    meaningful (prompts refer to "image 1", "image 2"); each input costs
    ~1.5k input image_tokens. Accepts {prompt, images | image, size?,
    quality?, background?, n?}; `images` entries may be data URLs, bare base64
    (assumed PNG) or {"image_url": ...}.
    """
    payload, error = _read_image_payload()
    if error is not None:
        return error
    common = _image_request_common(payload)
    if isinstance(common, str):
        return _err(400, common)
    body, n = common
    images = _normalize_input_images(payload)
    if isinstance(images, str):
        return _err(400, images)
    body["images"] = images
    log.info(
        "-> [images/edits] n=%s inputs=%s size=%s quality=%s prompt=%r",
        n, len(images), body["size"], body["quality"], body["prompt"][:80],
    )
    return _run_image_request("images/edits", body, n)


# Standalone web search. Codex CLI 0.158 (feature StandaloneWebSearch) stops
# sending the hosted `web_search` tool: the model calls a `web.run` function
# and the CLI itself does `POST {provider base_url}/alpha/search` with
# {id, model, input, commands, settings, ...}, expecting
# {"encrypted_output", "output", "results"} back (codex-rs/codex-api
# endpoint/search.rs). For a ChatGPT login the same path lives under
# CODEX_BASE; probed 2026-09-28 with a pool token and client version 0.153.4:
# 200 in ~1.5 s, 37 results. `model` is not validated upstream (a bogus slug
# also got 200) but is resolved like /v1/responses for consistency.
SEARCH_TIMEOUT = int(os.environ.get("CODEX_SEARCH_TIMEOUT", "120"))


@app.route("/v1/alpha/search", methods=["POST"])
@app.route("/alpha/search", methods=["POST"])
def alpha_search():
    """Codex standalone web search, forwarded to `{CODEX_BASE}/alpha/search`.

    The body goes upstream unchanged apart from `model`; the upstream JSON
    comes back verbatim so fields Codex does not know yet survive the trip.
    """
    payload, error = _read_image_payload()
    if error is not None:
        return error

    requested_model = payload.get("model") or "(unset)"
    resolved = _strip_window_suffix(str(requested_model))
    payload["model"] = resolved if resolved in _allowed_models() else CODEX_MODEL
    commands = payload.get("commands") if isinstance(payload.get("commands"), dict) else {}
    log.info(
        "-> [alpha/search] requested=%s served=%s queries=%s opens=%s",
        requested_model,
        payload["model"],
        len(commands.get("search_query") or []),
        len(commands.get("open") or []),
    )

    started = time.time()
    try:
        resp, account = _post_upstream_json("alpha/search", payload, SEARCH_TIMEOUT)
    except requests.RequestException as exc:
        log.error("[alpha/search] upstream connection failed: %s", exc)
        return _err(502, f"Upstream connection failed: {type(exc).__name__}")

    if resp.status_code != 200:
        detail = resp.text[:500]
        resp.close()
        log.error("[alpha/search] upstream HTTP %s: %s", resp.status_code, detail)
        return _err(resp.status_code, f"Upstream error {resp.status_code}: {detail}")

    content = resp.content
    content_type = resp.headers.get("Content-Type", "application/json")
    resp.close()
    try:
        results = len(json.loads(content).get("results") or [])
    except (ValueError, AttributeError):
        return _err(502, "Upstream search response was not valid JSON")
    log.info(
        "<- [alpha/search] account=%s results=%s %.1fs",
        account.id if account else "?", results, time.time() - started,
    )
    return Response(content, status=200, content_type=content_type)


@app.route("/health", methods=["GET"])
def health():
    info = {
        "status": "ok", "model": CODEX_MODEL, "auth_required": bool(BRIDGE_TOKEN),
        "client_version": CODEX_CLIENT_VERSION,
        "client_version_source": CODEX_CLIENT_VERSION_SOURCE,
    }
    try:
        account = pool.active()
        access, account_id = account.creds.get()
        info["codex_auth"] = "ok" if access and account_id else "incomplete"
        info["plan"] = account.creds.plan_type()
        info["account"] = account.id
        described = [pool.describe_one(a) for a in pool.accounts()]
        info["pool"] = [
            {
                "id": d["id"],
                "available": d.get("available", True),
                "cooldown_until_local": d.get("cooldown_until_local"),
                "cooldown_reason": d.get("cooldown_reason"),
            }
            for d in described
        ]
        # Holding a syntactically valid token is not the same as being able to
        # serve: every account can be parked (spent quota, revoked session) and
        # requests then fail. Health must not read green in that state.
        usable = [d for d in described if d.get("available", True)]
        if not usable:
            info["status"] = "degraded"
            info["detail"] = ("every account is parked: "
                              + "; ".join(f"{d['id']} {d.get('cooldown_reason', '?')} "
                                          f"until {d.get('cooldown_until_local', '?')}"
                                          for d in described))
    except Exception as exc:
        info["status"] = "degraded"
        info["codex_auth"] = f"error: {exc}"
    return jsonify(info)


def main() -> None:
    log.info("codex-bridge on %s:%s -> %s (model=%s, auth=%s, accounts=%s)",
             LISTEN_HOST, LISTEN_PORT, CODEX_BASE, CODEX_MODEL, bool(BRIDGE_TOKEN),
             " > ".join(a.id for a in pool.accounts()))
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
