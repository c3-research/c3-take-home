#!/usr/bin/env python3
"""Caching spike (B3 brief): two identical ~5k-token requests per format through a
RUNNING real proxy; the second must report cached tokens.

    python proxy/server.py &                   # real OpenRouter, key from .env
    python proxy/cache_spike.py [--url http://127.0.0.1:8787] [--out evidence/tests/cache-spike.json]

Mints a $0.50 token (case "cache-spike") via the admin API. Never touches the key.
Costs about $0.05. Exit 0 only if every format shows cached tokens on the repeat.
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import admin  # noqa: E402

# ~20k chars ≈ 5k tokens of stable, non-repetitive-looking text.
SYSTEM = "You are a terse assistant. Reference notes follow.\n" + "\n".join(
    f"Note {i}: component c{i % 37} depends on c{(i * 7) % 41}; lease epoch {i * 13 % 97}; "
    f"retry budget {i % 5}; owner team-{chr(65 + i % 26)}." for i in range(260))


def post(url, path, body, headers):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                 headers=dict(headers, **{"content-type": "application/json"}), method="POST")
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read()), time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=admin.DEFAULT_URL)
    ap.add_argument("--out", default=str(HERE.parent / "evidence" / "tests" / "cache-spike.json"))
    a = ap.parse_args()
    tok = admin.mint(0.50, case="cache-spike", fixer="b3", arm="spike", url=a.url)["token"]
    results = {}
    for fmt in ("openai", "anthropic"):
        rows = []
        for i in range(2):
            if fmt == "openai":
                body = {"model": "anthropic/claude-sonnet-4.6", "max_tokens": 5,
                        "messages": [{"role": "system", "content": SYSTEM},
                                     {"role": "user", "content": "Reply OK."}]}
                resp, dt = post(a.url, "/v1/chat/completions", body, {"authorization": f"Bearer {tok}"})
                u = resp.get("usage", {})
                d = u.get("prompt_tokens_details") or {}
                rows.append({"prompt_tokens": u.get("prompt_tokens"), "cached_tokens": d.get("cached_tokens"),
                             "cache_write_tokens": d.get("cache_write_tokens"), "cost": u.get("cost"),
                             "latency_s": round(dt, 2), "id": resp.get("id")})
            else:
                body = {"model": "claude-sonnet-4-6", "max_tokens": 5, "system": SYSTEM + "\n(anthropic)",
                        "messages": [{"role": "user", "content": "Reply OK."}]}
                resp, dt = post(a.url, "/v1/messages", body, {"x-api-key": tok, "anthropic-version": "2023-06-01"})
                u = resp.get("usage", {})
                rows.append({"input_tokens": u.get("input_tokens"),
                             "cache_read_input_tokens": u.get("cache_read_input_tokens"),
                             "cache_creation_input_tokens": u.get("cache_creation_input_tokens"),
                             "cost": u.get("cost"), "latency_s": round(dt, 2), "id": resp.get("id")})
            time.sleep(2)
        second = rows[1]
        cached = second.get("cached_tokens") or second.get("cache_read_input_tokens") or 0
        results[fmt] = {"requests": rows, "second_cached_tokens": cached, "ok": cached > 0}
    stats = admin.stats(tok, url=a.url)
    out = {"test": "cache-spike", "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "note": "Two identical ~5k-token requests per format, no client cache_control (proxy injects it).",
           "passed": all(r["ok"] for r in results.values()), "formats": results, "token_stats": stats}
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1))
    print(json.dumps({k: (v["second_cached_tokens"], v["ok"]) for k, v in results.items()}), "->", a.out)
    return 0 if out["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
