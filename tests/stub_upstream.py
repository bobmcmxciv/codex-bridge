"""Stand-in for https://chatgpt.com/backend-api/codex, for failover testing.

Answers the real `usage_limit_reached` 429 for whichever account labels are
listed in STUB_QUOTA_SPENT, and a normal Responses SSE stream for the rest, so
the switch can be exercised over real HTTP without burning a subscription.

    STUB_PORT=8913 STUB_QUOTA_SPENT=acct1 python tests/stub_upstream.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_auth  # noqa: E402

PORT = int(os.environ.get("STUB_PORT", "8913"))
SPENT = {s for s in os.environ.get("STUB_QUOTA_SPENT", "").split(",") if s}
CALLS_PATH = Path(os.environ.get("STUB_CALLS", Path(__file__).with_name("stub-calls.log")))

app = Flask(__name__)


def _label() -> str:
    bearer = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return fake_auth.label_of(bearer) or "unknown"


def _record(kind: str, label: str) -> None:
    with open(CALLS_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.time(), "kind": kind, "label": label}) + "\n")


@app.route("/responses", methods=["POST"])
def responses():
    label = _label()
    _record("responses", label)
    if label in SPENT:
        return (
            json.dumps({"error": {
                "type": "usage_limit_reached",
                "message": "The usage limit has been reached",
                "plan_type": "prolite",
                "resets_at": int(time.time()) + 5000,
                "eligible_promo": None,
                "resets_in_seconds": 5000,
            }}),
            429,
            {"Content-Type": "application/json"},
        )

    def stream():
        text = f"served-by-{label}"
        yield f'data: {json.dumps({"type": "response.output_text.delta", "delta": text})}\n\n'
        yield "data: " + json.dumps({
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 11, "output_tokens": 3, "total_tokens": 14,
                                   "input_tokens_details": {"cached_tokens": 0}}},
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    return Response(stream(), mimetype="text/event-stream")


@app.route("/wham/usage", methods=["GET"])
@app.route("/usage", methods=["GET"])
def usage():
    label = _label()
    _record("usage", label)
    spent = label in SPENT
    return jsonify({
        "email": f"{label}@example.invalid",
        "plan_type": "prolite" if spent else "pro",
        "rate_limit": {
            "allowed": not spent,
            "limit_reached": spent,
            "primary_window": {
                "limit_window_seconds": 604800,
                "reset_at": int(time.time()) + 5000,
                "used_percent": 100 if spent else 4,
            },
            "secondary_window": None,
        },
        "user_id": f"user-{label}",
    })


if __name__ == "__main__":
    # The call log is appended to, never truncated here: the harness restarts
    # this process mid-run (to refill the quota) and needs the history intact.
    app.run(host="127.0.0.1", port=PORT, threaded=True)
