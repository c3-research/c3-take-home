#!/usr/bin/env python3
"""Mint a per-case proxy token.

    python proxy/mint.py --budget USD --case ID --fixer ID --arm ARM [--run N]
    python proxy/mint.py --budget 40 --case build-G --fixer G --allow-model anthropic/claude-opus-5.5

Default model allowlist: anthropic/claude-sonnet-4.6 only. --allow-model (repeatable)
replaces it. Allowing anthropic/claude-opus-5.5 makes a designer-build token
: Opus 5.5 only, arm forced to designer-build.

Prints a JSON summary line, then the token as the LAST line of stdout.
Proxy location and admin secret come from C3_PROXY_PORT / C3_PROXY_ADMIN_URL and
C3_PROXY_STATE (see admin.py), or --url / --state.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import admin  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=float, required=True)
    ap.add_argument("--case")
    ap.add_argument("--fixer")
    ap.add_argument("--arm")
    ap.add_argument("--run")
    ap.add_argument("--allow-model", action="append", dest="models", metavar="MODEL")
    ap.add_argument("--url", default=admin.DEFAULT_URL)
    ap.add_argument("--state", help="proxy state dir holding admin.token")
    a = ap.parse_args()
    secret = admin._secret(str(Path(a.state) / "admin.token") if a.state else None)
    out = admin.mint(a.budget, a.case, a.fixer, a.arm, a.run, a.url, secret, a.models)
    tok = out.pop("token")
    print(json.dumps(out))
    print(tok)


if __name__ == "__main__":
    main()
