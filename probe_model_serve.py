"""Which upstream slugs actually serve a request on this subscription?

The catalog at /backend-api/codex/models advertises slugs plus a
`supported_in_api` flag, but that flag describes the OpenAI API, not the Codex
responses backend this bridge talks to. The only way to know whether a slug is
usable here is to send it a real (tiny) request, which is what this does.

Usage: python probe_model_serve.py [slug ...]   (default: whole catalog)
Each probe consumes a request against the active account's quota.
"""
import sys

import codex_bridge as cb


def probe(slug: str) -> str:
    payload = cb.translate_request({
        "model": "placeholder-overridden-below",
        "messages": [{"role": "user", "content": "reply with just OK"}],
    })
    payload["model"] = slug
    # A fresh affinity key per slug: reusing one would let a cache-pinned node
        # answer for a model it was not asked about.
    payload["prompt_cache_key"] = f"probe-model-serve-{slug}"
    try:
        resp, account = cb.call_upstream(payload)
    except Exception as exc:
        return f"EXC {type(exc).__name__}: {exc}"
    if resp.status_code != 200:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    kind, err, _buffered = cb._peek_stream(cb.iter_events(resp))
    resp.close()
    if kind == "content":
        return f"OK (account={account.id if account else '?'})"
    if kind == "empty":
        return "EMPTY stream"
    code, msg = cb._upstream_error_info(err)
    return f"REFUSED {code}: {msg[:200]}"


def main() -> None:
    slugs = sys.argv[1:]
    if not slugs:
        catalog = cb._model_catalog() if hasattr(cb, "_model_catalog") else None
        if not catalog:
            print("no catalog available; pass slugs explicitly", file=sys.stderr)
            raise SystemExit(2)
        slugs = [m["slug"] for m in catalog]
    for slug in slugs:
        print(f"{slug:24s} {probe(slug)}", flush=True)


if __name__ == "__main__":
    main()
