#!/usr/bin/env python3
"""Admin client for the proxy (for the grader and humans).

    python proxy/admin.py mint --budget 6 --case C2 --fixer naive-claude --arm base [--run 1]
    python proxy/admin.py stats <token-or-id>
    python proxy/admin.py list
    python proxy/admin.py revoke <token-or-id>

Prints JSON. `mint` output includes the token. Reads the admin secret from
<state>/admin.token, where state is C3_PROXY_STATE or proxy/.state (override
with --admin-token-file or C3_PROXY_ADMIN_TOKEN). The proxy URL is
C3_PROXY_ADMIN_URL, else http://127.0.0.1:$C3_PROXY_PORT (default 8787).
For the grader, proxy/mint.py prints just the token as its last stdout line.

Library use: from proxy.admin import mint, stats, revoke
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_URL = os.environ.get("C3_PROXY_ADMIN_URL",
                             f"http://127.0.0.1:{os.environ.get('C3_PROXY_PORT', '8787')}")
STATE_DIR = Path(os.environ.get("C3_PROXY_STATE") or HERE / ".state")


def _secret(path: str | None = None) -> str:
    if os.environ.get("C3_PROXY_ADMIN_TOKEN"):
        return os.environ["C3_PROXY_ADMIN_TOKEN"]
    return Path(path or STATE_DIR / "admin.token").read_text().strip()


def _call(method: str, path: str, body=None, url=DEFAULT_URL, secret=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method=method,
                                 headers={"X-Admin-Token": secret or _secret(),
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"proxy admin {method} {path}: HTTP {e.code}: {e.read()[:300]!r}") from None


def mint(budget_usd: float, case=None, fixer=None, arm=None, run=None, url=DEFAULT_URL, secret=None,
         models=None) -> dict:
    """models: list of allowed model ids (default: Sonnet 4.6 only). Allowing
    anthropic/claude-opus-5.5 makes a designer-build token (Opus only, arm=designer-build)."""
    body = {"budget_usd": budget_usd, "case": case, "fixer": fixer, "arm": arm, "run": run}
    if models:
        body["models"] = list(models)
    return _call("POST", "/admin/tokens", body, url, secret)


def stats(ref: str, url=DEFAULT_URL, secret=None) -> dict:
    return _call("GET", f"/admin/tokens/{ref}", None, url, secret)


def revoke(ref: str, url=DEFAULT_URL, secret=None) -> dict:
    return _call("POST", f"/admin/tokens/{ref}/revoke", {}, url, secret)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--admin-token-file")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint")
    m.add_argument("--budget", type=float, required=True)
    m.add_argument("--case")
    m.add_argument("--fixer")
    m.add_argument("--arm")
    m.add_argument("--run")
    m.add_argument("--allow-model", action="append", dest="models")
    for name in ("stats", "revoke"):
        s = sub.add_parser(name)
        s.add_argument("ref")
    sub.add_parser("list")
    a = ap.parse_args()
    secret = _secret(a.admin_token_file)
    if a.cmd == "mint":
        out = mint(a.budget, a.case, a.fixer, a.arm, a.run, a.url, secret, a.models)
    elif a.cmd == "stats":
        out = stats(a.ref, a.url, secret)
    elif a.cmd == "revoke":
        out = revoke(a.ref, a.url, secret)
    else:
        out = _call("GET", "/admin/tokens", None, a.url, secret)
    json.dump(out, sys.stdout, indent=2)
    print()


if __name__ == "__main__":
    main()
