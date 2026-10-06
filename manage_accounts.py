"""Maintain the codex-bridge account pool.

Typical flow for adding a second ChatGPT subscription:

    # 1. get the live login out of ~/.codex/auth.json and into the pool.
    #    `codex login` REVOKES the session it finds there, at OpenAI - copying
    #    the file is not protection, the file has to be gone.
    python manage_accounts.py detach

    # 2. authorise the other account
    codex login --device-auth

    # 3. file it into the pool as the highest-priority account
    python manage_accounts.py import --id prolite-old --first

    # 4. optional: give the codex CLI on this box a login again
    python manage_accounts.py attach <id>

pool.json edits are picked up live; no restart needed.

Commands: list | detach | attach | import | backup | restore | order | park | clear | remove
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from codex_auth import default_auth_path, jwt_claims  # noqa: E402
from pool_paths import ACCOUNTS_DIR, BACKUP_DIR  # noqa: E402
from pool_paths import ACCOUNTS_CONFIG as POOL_PATH  # noqa: E402
from pool_paths import ACCOUNTS_STATE as STATE_PATH  # noqa: E402

AUTH_CLAIM = "https://api.openai.com/auth"


def _read(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _identity(path: Path) -> dict:
    data = _read(path)
    tokens = data.get("tokens") or {}
    claims = jwt_claims(tokens.get("id_token", "") or "")
    auth = claims.get(AUTH_CLAIM) or {}
    return {
        "email": claims.get("email", ""),
        "plan": auth.get("chatgpt_plan_type", "unknown"),
        "account_id": tokens.get("account_id") or auth.get("chatgpt_account_id", ""),
        "auth_mode": data.get("auth_mode", ""),
    }


def _load_pool() -> dict:
    try:
        return _read(POOL_PATH)
    except FileNotFoundError:
        return {"accounts": []}


def _load_state() -> dict:
    try:
        return _read(STATE_PATH)
    except Exception:
        return {}


# -- commands ---------------------------------------------------------------

def cmd_list(args) -> int:
    pool = _load_pool()
    state = _load_state()
    entries = pool.get("accounts") or []
    if not entries:
        print(f"no accounts configured in {POOL_PATH}")
        return 1
    now = time.time()
    for i, spec in enumerate(entries):
        path = Path(spec.get("path") or default_auth_path())
        try:
            ident = _identity(path)
            desc = f"{ident['plan']:<8} {ident['email'] or '(no email claim)'}"
            if spec.get("account_id") and ident["account_id"] != spec["account_id"]:
                desc += "  !! account_id MISMATCH: file holds " + ident["account_id"]
        except Exception as exc:
            desc = f"UNREADABLE: {exc}"
        st = state.get(spec.get("id")) or {}
        until = float(st.get("cooldown_until") or 0)
        if until > now:
            status = (
                f"parked {st.get('reason', '')} until "
                f"{time.strftime('%m-%d %H:%M', time.localtime(until))}"
                f" ({(until - now) / 3600:.1f}h)"
            )
        else:
            status = "ready"
        print(f"{i}. {spec.get('id'):<18} {status:<38} {desc}")
        print(f"   {path}")
        if spec.get("note"):
            print(f"   {spec['note']}")
    return 0


def cmd_backup(args) -> int:
    src = Path(args.source) if args.source else default_auth_path()
    ident = _identity(src)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = (ident["account_id"] or "unknown")[:8]
    dest = BACKUP_DIR / f"auth-{stamp}-{ident['plan']}-{tag}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    print(f"backed up {src}\n       -> {dest}\n       ({ident['plan']}, {ident['email']})")
    return 0


def cmd_restore(args) -> int:
    src = Path(args.backup)
    dest = Path(args.into_cli) if args.into_cli else default_auth_path()
    ident = _identity(src)
    shutil.copy2(src, dest)
    print(f"restored {ident['plan']} {ident['email']}\n      -> {dest}")
    return 0


def cmd_import(args) -> int:
    src = Path(args.source) if args.source else default_auth_path()
    ident = _identity(src)
    if ident["auth_mode"] != "chatgpt":
        print(f"refusing: {src} is auth_mode={ident['auth_mode']!r}, not a ChatGPT login")
        return 2
    if not ident["account_id"]:
        print(f"refusing: no chatgpt_account_id in {src}")
        return 2

    pool = _load_pool()
    entries = pool.get("accounts") or []
    for spec in entries:
        if spec.get("account_id") == ident["account_id"]:
            print(f"account {ident['account_id']} is already in the pool as {spec['id']!r}")
            return 2

    account_id = args.id or f"{ident['plan']}-{ident['account_id'][:8]}"
    if args.in_place:
        dest = src
    else:
        dest = ACCOUNTS_DIR / f"{account_id}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    spec = {
        "id": account_id,
        # Absolute: the service runs as LocalSystem from its own AppDirectory,
        # so a path relative to whoever ran this command would be a trap.
        "path": str(dest.resolve()),
        "account_id": ident["account_id"],
        "note": args.note or f"{ident['plan']}, {ident['email']}, imported {time.strftime('%Y-%m-%d')}",
    }
    entries.insert(0, spec) if args.first else entries.append(spec)
    pool["accounts"] = entries
    _write(POOL_PATH, pool)
    print(f"added {account_id} ({ident['plan']}, {ident['email']}) at position "
          f"{0 if args.first else len(entries) - 1}")
    print(f"credentials: {dest}")
    return cmd_list(args)


def cmd_detach(args) -> int:
    """Move the codex CLI's login into the pool and remove ~/.codex/auth.json.

    `codex login` revokes the session it finds there, at OpenAI - a file copy
    does not survive that. Leaving nothing to revoke is the only protection.
    """
    cli_path = Path(args.cli_path) if args.cli_path else default_auth_path()
    if not cli_path.exists():
        print(f"{cli_path} is already absent - safe to run `codex login`")
        return 0

    ident = _identity(cli_path)
    pool = _load_pool()
    entries = pool.get("accounts") or []
    spec = next((s for s in entries if s.get("account_id") == ident["account_id"]), None)
    account_id = (spec or {}).get("id") or args.id or \
        f"{ident['plan']}-{(ident['account_id'] or 'unknown')[:8]}"
    dest = (ACCOUNTS_DIR / f"{account_id}.json").resolve()

    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # Re-logging an account that is already in the pool: the file sitting
        # there holds the revoked session, and the fresh one replaces it.
        print(f"replacing the previous credentials at {dest}")
        dest.unlink()
    shutil.move(str(cli_path), str(dest))

    if spec is None:
        entries.append({"id": account_id, "path": str(dest),
                        "account_id": ident["account_id"],
                        "note": f"{ident['plan']}, {ident['email']}, "
                                f"detached {time.strftime('%Y-%m-%d')}"})
    else:
        spec["path"] = str(dest)
    pool["accounts"] = entries
    _write(POOL_PATH, pool)
    print(f"moved {ident['plan']} {ident['email']}\n   -> {dest}")
    print(f"{cli_path} is gone; `codex login` now has no session to revoke")
    return cmd_list(args)


def cmd_attach(args) -> int:
    """Hand a pool account's file back to the codex CLI."""
    cli_path = Path(args.cli_path) if args.cli_path else default_auth_path()
    if cli_path.exists():
        print(f"refusing: {cli_path} already exists - run `detach` first")
        return 2
    pool = _load_pool()
    spec = next((s for s in pool.get("accounts") or [] if s.get("id") == args.id), None)
    if spec is None:
        print(f"no account {args.id!r} in the pool")
        return 2
    src = Path(spec["path"])
    if src == cli_path:
        print(f"{args.id} already serves from {cli_path}")
        return 0
    cli_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(cli_path))
    spec["path"] = str(cli_path)
    _write(POOL_PATH, pool)
    print(f"{args.id} now serves from {cli_path} (shared with the codex CLI)")
    print("careful: the next `codex login` on this box would revoke it - detach first")
    return cmd_list(args)


def cmd_order(args) -> int:
    pool = _load_pool()
    by_id = {spec.get("id"): spec for spec in pool.get("accounts") or []}
    missing = [i for i in args.ids if i not in by_id]
    if missing:
        print(f"unknown account id(s): {', '.join(missing)}")
        return 2
    rest = [spec for spec in pool["accounts"] if spec.get("id") not in args.ids]
    pool["accounts"] = [by_id[i] for i in args.ids] + rest
    _write(POOL_PATH, pool)
    return cmd_list(args)


def cmd_park(args) -> int:
    """Take an account out of service by hand, as a spent quota would.

    Useful to force traffic onto another subscription without waiting for a
    429, and to rehearse the switch. The service re-reads state.json, so it
    takes effect on the next request.
    """
    pool = _load_pool()
    if not any(s.get("id") == args.id for s in pool.get("accounts") or []):
        print(f"no account {args.id!r} in the pool")
        return 2
    state = _load_state()
    until = time.time() + args.hours * 3600
    state[args.id] = {
        "cooldown_until": int(until),
        "reason": args.reason,
        "detail": "parked by hand via manage_accounts.py",
        "since": int(time.time()),
    }
    _write(STATE_PATH, state)
    print(f"parked {args.id} for {args.hours}h "
          f"(until {time.strftime('%m-%d %H:%M', time.localtime(until))})")
    return cmd_list(args)


def cmd_clear(args) -> int:
    state = _load_state()
    if args.id not in state:
        print(f"{args.id} has no cooldown recorded")
        return 0
    state.pop(args.id)
    _write(STATE_PATH, state)
    print(f"cleared cooldown for {args.id}")
    return cmd_list(args)


def cmd_remove(args) -> int:
    pool = _load_pool()
    entries = pool.get("accounts") or []
    kept = [spec for spec in entries if spec.get("id") != args.id]
    if len(kept) == len(entries):
        print(f"no account {args.id!r} in the pool")
        return 2
    pool["accounts"] = kept
    _write(POOL_PATH, pool)
    print(f"removed {args.id} from the pool (its credential file was left in place)")
    return cmd_list(args)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show the pool and its cooldowns").set_defaults(func=cmd_list)

    p = sub.add_parser("backup", help="copy a codex login file into accounts/backups/")
    p.add_argument("--source", help="default: the codex CLI's own auth.json")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="copy a backup back over a codex login file")
    p.add_argument("backup")
    p.add_argument("--into-cli", help="default: the codex CLI's own auth.json")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("import", help="file a codex login into the pool")
    p.add_argument("--id", help="pool id; default <plan>-<account_id prefix>")
    p.add_argument("--source", help="default: the codex CLI's own auth.json")
    p.add_argument("--note")
    p.add_argument("--first", action="store_true", help="make it the primary account")
    p.add_argument("--in-place", action="store_true",
                   help="register the source path itself instead of copying it")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("detach", help="move the CLI's login into the pool, so a "
                                      "`codex login` cannot revoke it")
    p.add_argument("--id", help="pool id to give it, if it is not in the pool yet")
    p.add_argument("--cli-path", help="default: the codex CLI's own auth.json")
    p.set_defaults(func=cmd_detach)

    p = sub.add_parser("attach", help="hand a pool account's file back to the codex CLI")
    p.add_argument("id")
    p.add_argument("--cli-path", help="default: the codex CLI's own auth.json")
    p.set_defaults(func=cmd_attach)

    p = sub.add_parser("order", help="set priority order, highest first")
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=cmd_order)

    p = sub.add_parser("park", help="take an account out of service by hand")
    p.add_argument("id")
    p.add_argument("--hours", type=float, default=1.0)
    p.add_argument("--reason", default="parked_by_hand")
    p.set_defaults(func=cmd_park)

    p = sub.add_parser("clear", help="drop an account's cooldown")
    p.add_argument("id")
    p.set_defaults(func=cmd_clear)

    p = sub.add_parser("remove", help="drop an account from the pool")
    p.add_argument("id")
    p.set_defaults(func=cmd_remove)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
