"""Where the account pool lives.

Its own module so the service, the probes, and manage_accounts.py all resolve
the same files without importing the Flask app for two constants. `.env` is
loaded here too - `load_dotenv` never overrides a variable that is already set,
so the service's own load stays authoritative.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env", verbose=False)

ACCOUNTS_DIR = Path(os.environ.get("CODEX_ACCOUNTS_DIR", APP_DIR / "accounts"))
ACCOUNTS_CONFIG = Path(os.environ.get("CODEX_ACCOUNTS_CONFIG", ACCOUNTS_DIR / "pool.json"))
ACCOUNTS_STATE = Path(os.environ.get("CODEX_ACCOUNTS_STATE", ACCOUNTS_DIR / "state.json"))
BACKUP_DIR = ACCOUNTS_DIR / "backups"
