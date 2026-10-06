"""Ordered pool of Codex ChatGPT logins with quota-aware failover.

The bridge used to hold exactly one `codex login` session (`~/.codex/auth.json`).
When that subscription hits its window limit the upstream answers

    429 {"error":{"type":"usage_limit_reached","plan_type":"pro",
                  "resets_at":1785912763,"resets_in_seconds":521062}}

and from then on every request fails until the window resets - days, on a weekly
window. This module keeps several logins side by side, always serves the first
one that is not known-exhausted, and parks an exhausted account until its own
`resets_at`, so coming back is automatic and needs no restart.

Layout (paths overridable via .env):

    accounts/pool.json    ordered account list - priority is array order
    accounts/state.json   runtime cooldowns, rewritten whenever state changes
    accounts/<id>.json    one `codex login` credential file per account

A pool entry may point at `~/.codex/auth.json`, which is what the `codex` CLI on
this box keeps using. Sharing that one file is deliberate: OpenAI rotates the
refresh token on every refresh, so two copies of the *same* account would fight
and one of them would end up holding a dead token.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

from codex_auth import CodexCredentials, default_auth_path, jwt_claims

log = logging.getLogger("codex-bridge.accounts")

# `resets_at` comes from the upstream; a bogus value must not park an account
# forever. Longest real window seen on these plans is 7 days.
MAX_COOLDOWN = 8 * 24 * 3600
# Used when the upstream says "limit reached" without saying when it lifts.
DEFAULT_QUOTA_COOLDOWN = 3600
# Credential / auth failures are not quota failures; retry them soon.
AUTH_COOLDOWN = 600
# 429s that are not usage_limit_reached are short-term throttling.
THROTTLE_COOLDOWN = 60

AUTH_CLAIM = "https://api.openai.com/auth"


def _read_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


# Plans whose logins the Codex backend refuses to serve models for.
SKIP_PLANS = frozenset({"free"})


class Account:
    """One `codex login` credential file plus its place in the priority order."""

    def __init__(self, spec: dict, index: int):
        self.priority = index
        self.id = str(spec.get("id") or f"account-{index + 1}")
        raw = str(spec.get("path") or "").strip()
        self.path = (
            Path(os.path.expandvars(raw)).expanduser() if raw else default_auth_path()
        )
        self.note = str(spec.get("note") or "")
        # Recorded at pool-build time. If the file is later re-logged into a
        # different ChatGPT account (e.g. someone runs `codex login` again), the
        # mismatch shows up in /accounts instead of silently changing identity.
        self.expect_account_id = str(spec.get("account_id") or "")
        self.creds = CodexCredentials(self.path)

    def plan(self) -> str:
        """ChatGPT plan claim from the credential file, cached on its mtime.

        Used to keep free-tier logins out of the rotation: the Codex backend
        answers their model requests with `400 ... not supported when using
        Codex with a ChatGPT account`, which the bridge would pass through as
        a client error instead of failing over.
        """
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return "unknown"
        cached = getattr(self, "_plan_cache", None)
        if cached and cached[0] == mtime:
            return cached[1]
        plan = str(self.identity().get("plan") or "unknown")
        self._plan_cache = (mtime, plan)
        return plan

    def identity(self) -> dict:
        info: dict = {
            "id": self.id,
            "priority": self.priority,
            "path": str(self.path),
            "note": self.note,
        }
        try:
            data = _read_json(self.path)
        except Exception as exc:
            info["error"] = f"unreadable: {exc}"
            return info
        tokens = data.get("tokens") or {}
        claims = jwt_claims(tokens.get("id_token", "") or "")
        auth = claims.get(AUTH_CLAIM) or {}
        info["email"] = claims.get("email", "")
        info["plan"] = auth.get("chatgpt_plan_type", "unknown")
        info["account_id"] = tokens.get("account_id") or auth.get("chatgpt_account_id", "")
        info["auth_mode"] = data.get("auth_mode", "")
        info["last_refresh"] = data.get("last_refresh", "")
        if (
            self.expect_account_id
            and info["account_id"]
            and info["account_id"] != self.expect_account_id
        ):
            info["identity_mismatch"] = True
            info["expected_account_id"] = self.expect_account_id
        return info

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Account {self.id} {self.path}>"


class AccountPool:
    """Priority-ordered accounts with persisted cooldowns."""

    def __init__(self, config_path: Path | str, state_path: Path | str):
        self._skip_logged: set[str] = set()
        self.config_path = Path(config_path)
        self.state_path = Path(state_path)
        self._lock = threading.RLock()
        self._accounts: list[Account] = []
        self._config_mtime: float = -1.0
        self._state: dict = {}
        self._load_state()
        with self._lock:
            self._load_config_locked()

    # -- config -------------------------------------------------------------

    def _load_config_locked(self) -> None:
        """(Re)read pool.json. Editing it takes effect without a restart."""
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError:
            if not self._accounts:
                # No pool configured: behave exactly like the single-login bridge.
                self._accounts = [Account({"id": "default"}, 0)]
                log.info("no pool config at %s, using %s", self.config_path,
                         self._accounts[0].path)
            return
        if mtime == self._config_mtime and self._accounts:
            return
        try:
            data = _read_json(self.config_path)
            specs = [s for s in (data.get("accounts") or []) if s.get("enabled", True)]
            accounts = [Account(spec, i) for i, spec in enumerate(specs)]
        except Exception as exc:
            log.error("pool config %s is unusable (%s); keeping previous pool",
                      self.config_path, exc)
            if not self._accounts:
                self._accounts = [Account({"id": "default"}, 0)]
            return
        if not accounts:
            log.error("pool config %s lists no enabled account; keeping previous pool",
                      self.config_path)
            if not self._accounts:
                self._accounts = [Account({"id": "default"}, 0)]
            return
        self._accounts = accounts
        self._config_mtime = mtime
        log.info("account pool (%d): %s", len(accounts),
                 " > ".join(a.id for a in accounts))

    def accounts(self) -> list[Account]:
        with self._lock:
            self._load_config_locked()
            return list(self._accounts)

    # -- state --------------------------------------------------------------

    def _load_state(self) -> None:
        try:
            self._state = _read_json(self.state_path)
            self._state_mtime = self.state_path.stat().st_mtime
        except Exception:
            self._state = {}
            self._state_mtime = -1.0
        if not isinstance(self._state, dict):
            self._state = {}

    def _refresh_state_locked(self) -> None:
        """Pick up state written by another process.

        `manage_accounts.py clear` releases an account by rewriting state.json
        while the service is running; without this the service would keep the
        cooldown in memory and the command would look like it did nothing.
        """
        try:
            mtime = self.state_path.stat().st_mtime
        except OSError:
            return
        if mtime != getattr(self, "_state_mtime", -1.0):
            self._load_state()

    def _save_state_locked(self) -> None:
        try:
            _write_json_atomic(self.state_path, self._state)
            self._state_mtime = self.state_path.stat().st_mtime
        except Exception as exc:
            log.error("could not persist account state to %s: %s", self.state_path, exc)

    def _cooldown_until(self, account_id: str) -> float:
        entry = self._state.get(account_id) or {}
        try:
            return float(entry.get("cooldown_until") or 0)
        except (TypeError, ValueError):
            return 0.0

    # -- selection ----------------------------------------------------------

    def candidates(self) -> list[Account]:
        """Accounts to try, best first.

        Ready accounts come first in configured priority order; accounts still
        on cooldown follow, soonest-to-recover first. Cooling accounts stay in
        the list on purpose - if every account is parked, one 429 answer beats
        refusing to call upstream at all, and a window may have lifted early.
        """
        with self._lock:
            self._load_config_locked()
            self._refresh_state_locked()
            now = time.time()
            ready: list[Account] = []
            cooling: list[tuple[float, Account]] = []
            for acc in self._accounts:
                until = self._cooldown_until(acc.id)
                if until <= now:
                    ready.append(acc)
                else:
                    cooling.append((until, acc))
            cooling.sort(key=lambda pair: pair[0])
            ordered = ready + [acc for _, acc in cooling]
            # Free-tier logins cannot serve any model on this backend; skip
            # them unless they are all we have (then a clear upstream error
            # still beats refusing to call at all).
            usable = [acc for acc in ordered if acc.plan() not in SKIP_PLANS]
            for acc in ordered:
                if acc not in usable and acc.id not in self._skip_logged:
                    self._skip_logged.add(acc.id)
                    log.warning("account %s is on plan %r; skipped for serving",
                                acc.id, acc.plan())
            return usable or ordered

    def active(self) -> Account:
        return self.candidates()[0]

    # -- state transitions --------------------------------------------------

    def _set_cooldown(self, account: Account, until: float, reason: str, detail: str) -> None:
        with self._lock:
            self._state[account.id] = {
                "cooldown_until": int(until),
                "reason": reason,
                "detail": detail[:300],
                "since": int(time.time()),
            }
            self._save_state_locked()

    def mark_quota(self, account: Account, resets_at=None, resets_in=None, detail: str = "") -> None:
        now = time.time()
        until = None
        try:
            if resets_at and float(resets_at) > now:
                until = float(resets_at)
            elif resets_in and float(resets_in) > 0:
                until = now + float(resets_in)
        except (TypeError, ValueError):
            until = None
        if until is None:
            until = now + DEFAULT_QUOTA_COOLDOWN
        until = min(until, now + MAX_COOLDOWN)
        self._set_cooldown(account, until, "usage_limit_reached", detail)
        log.warning(
            "account %s hit its usage limit; parked for %.1f h (until %s)",
            account.id, (until - now) / 3600.0,
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until)),
        )

    def mark_unavailable(self, account: Account, seconds: float, reason: str, detail: str = "") -> None:
        until = time.time() + max(0.0, float(seconds))
        self._set_cooldown(account, until, reason, detail)
        log.warning("account %s unavailable (%s); parked for %ds",
                    account.id, reason, int(seconds))

    def mark_ok(self, account: Account) -> None:
        """Clear a cooldown after the account has actually served a request."""
        with self._lock:
            if account.id in self._state:
                self._state.pop(account.id, None)
                self._save_state_locked()
                log.info("account %s is healthy again", account.id)

    def note_usage(self, account: Account, body: dict) -> None:
        """Feed a wham/usage payload into the pool state.

        Lets an account be parked (or released) from the usage endpoint alone,
        without waiting for a request to burn a 429.
        """
        if not isinstance(body, dict):
            return
        rate = body.get("rate_limit") or {}
        primary = rate.get("primary_window") or {}
        reached = bool(rate.get("limit_reached")) or rate.get("allowed") is False
        if reached:
            self.mark_quota(account, resets_at=primary.get("reset_at"),
                            detail="wham/usage reports limit_reached")
            return
        used = primary.get("used_percent")
        if isinstance(used, (int, float)) and used < 100:
            with self._lock:
                entry = self._state.get(account.id) or {}
                if entry.get("reason") == "usage_limit_reached":
                    self._state.pop(account.id, None)
                    self._save_state_locked()
                    log.info("account %s window has reset (used %.1f%%)", account.id, used)

    # -- reporting ----------------------------------------------------------

    def describe_one(self, account: Account) -> dict:
        info = account.identity()
        with self._lock:
            self._refresh_state_locked()
            entry = dict(self._state.get(account.id) or {})
        now = time.time()
        until = float(entry.get("cooldown_until") or 0)
        info["available"] = until <= now
        if account.plan() in SKIP_PLANS:
            info["available"] = False
            info["skipped"] = f"plan {account.plan()!r} cannot serve Codex models"
        if until > now:
            info["cooldown_until"] = int(until)
            info["cooldown_until_local"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(until)
            )
            info["cooldown_seconds_left"] = int(until - now)
            info["cooldown_reason"] = entry.get("reason", "")
            info["cooldown_detail"] = entry.get("detail", "")
        return info

    def describe(self) -> list[dict]:
        return [self.describe_one(acc) for acc in self.accounts()]
