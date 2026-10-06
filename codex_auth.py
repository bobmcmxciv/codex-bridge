"""
Codex ChatGPT-OAuth credential handling.

Reads ~/.codex/auth.json (written by `codex login`), transparently refreshes the
access token against https://auth.openai.com/oauth/token when it is close to
expiry, and writes the refreshed tokens back so the Codex CLI stays in sync.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path

import requests

# Public OAuth client id used by the Codex CLI. Present as the `aud` claim of the
# id_token that `codex login` stores.
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"

# Refresh this many seconds before the token actually expires.
REFRESH_SKEW = 300


def default_auth_path() -> Path:
    override = os.environ.get("CODEX_HOME")
    base = Path(override) if override else Path.home() / ".codex"
    return base / "auth.json"


def _b64url_json(segment: str) -> dict:
    segment += "=" * (-len(segment) % 4)
    return json.loads(base64.urlsafe_b64decode(segment))


def jwt_claims(token: str) -> dict:
    try:
        return _b64url_json(token.split(".")[1])
    except Exception:
        return {}


def jwt_expiry(token: str) -> int:
    exp = jwt_claims(token).get("exp")
    return int(exp) if isinstance(exp, (int, float)) else 0


class CodexCredentials:
    """Thread-safe view over ~/.codex/auth.json with lazy refresh."""

    def __init__(self, auth_path: Path | None = None):
        self.auth_path = auth_path or default_auth_path()
        self._lock = threading.Lock()
        self._data: dict = {}
        self._mtime: float = -1.0

    # -- disk ---------------------------------------------------------------

    def _load_locked(self) -> None:
        try:
            mtime = self.auth_path.stat().st_mtime
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Codex auth file not found at {self.auth_path}. Run `codex login` first."
            ) from exc
        if mtime != self._mtime or not self._data:
            with open(self.auth_path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
            self._mtime = mtime

    def _save_locked(self) -> None:
        tmp = self.auth_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self.auth_path)
        try:
            self._mtime = self.auth_path.stat().st_mtime
        except OSError:
            self._mtime = -1.0

    # -- refresh ------------------------------------------------------------

    def _refresh_locked(self) -> None:
        tokens = self._data.get("tokens") or {}
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            raise RuntimeError("No refresh_token in auth.json; run `codex login` again.")

        resp = requests.post(
            TOKEN_URL,
            json={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "scope": "openid profile email",
            },
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Token refresh failed with HTTP {resp.status_code}: {resp.text[:200]}"
            )
        payload = resp.json()

        tokens["access_token"] = payload.get("access_token", tokens.get("access_token"))
        tokens["id_token"] = payload.get("id_token", tokens.get("id_token"))
        # OpenAI rotates refresh tokens; keep the new one when present.
        if payload.get("refresh_token"):
            tokens["refresh_token"] = payload["refresh_token"]

        self._data["tokens"] = tokens
        self._data["last_refresh"] = (
            time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000000000Z"
        )
        self._save_locked()

    # -- public -------------------------------------------------------------

    def get(self, force_refresh: bool = False) -> tuple[str, str]:
        """Return (access_token, chatgpt_account_id), refreshing if needed."""
        with self._lock:
            self._load_locked()
            tokens = self._data.get("tokens") or {}
            access = tokens.get("access_token", "")

            if force_refresh or not access or jwt_expiry(access) - REFRESH_SKEW <= time.time():
                self._refresh_locked()
                tokens = self._data["tokens"]
                access = tokens["access_token"]

            account_id = tokens.get("account_id") or ""
            if not account_id:
                claims = jwt_claims(tokens.get("id_token", ""))
                auth_claim = claims.get("https://api.openai.com/auth") or {}
                account_id = auth_claim.get("chatgpt_account_id", "")
            return access, account_id

    def plan_type(self) -> str:
        with self._lock:
            self._load_locked()
            claims = jwt_claims((self._data.get("tokens") or {}).get("id_token", ""))
            return (claims.get("https://api.openai.com/auth") or {}).get(
                "chatgpt_plan_type", "unknown"
            )
