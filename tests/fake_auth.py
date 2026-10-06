"""Build auth.json files that look like a `codex login` result, for tests.

The tokens are unsigned - nothing in the bridge verifies signatures, it only
base64-decodes the payload - but the `exp` is real, so credential loading does
not try to refresh against the network.
"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path


def _b64(obj: dict) -> str:
    raw = json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def jwt(payload: dict) -> str:
    return f"{_b64({'alg': 'none'})}.{_b64(payload)}.sig"


def auth_json(label: str, plan: str = "pro", email: str | None = None,
              ttl: int = 30 * 24 * 3600) -> dict:
    account_id = f"{label}-0000-0000-0000-000000000000"
    claim = {
        "chatgpt_account_id": account_id,
        "chatgpt_plan_type": plan,
        "chatgpt_user_id": f"user-{label}",
    }
    exp = int(time.time()) + ttl
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {
            "id_token": jwt({
                "email": email or f"{label}@example.invalid",
                "exp": exp,
                "https://api.openai.com/auth": claim,
            }),
            # `label` travels in the access token so a stub upstream can tell
            # which account a request was made with.
            "access_token": jwt({"exp": exp, "label": label,
                                 "https://api.openai.com/auth": claim}),
            "refresh_token": f"rt-{label}",
            "account_id": account_id,
        },
        "last_refresh": "2026-08-01T00:00:00.000000000Z",
    }


def write(path: Path, label: str, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(auth_json(label, **kwargs), indent=2), encoding="utf-8")
    return path


def label_of(bearer: str) -> str:
    """Read the label back out of an access token (stub-upstream side)."""
    try:
        seg = bearer.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg)).get("label", "")
    except Exception:
        return ""
