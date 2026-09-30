#!/usr/bin/env python3
"""Trivial fixer (T6 plumbing): applies one fixed, harmless edit.

It appends a marker comment to the first Python file under $C3_WORKSPACE/src
(sorted by path), prints the contract environment, and pings the proxy's
/v1/models endpoint with its token (no model spend). Exits 0 when the edit was
made, 3 if a contract variable is missing.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

MARK = "# c3: trivial fixer edit\n"
need = ("C3_WORKSPACE", "C3_CORPUS", "C3_DEADLINE", "C3_BUDGET_USD", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL",
        "OPENROUTER_API_KEY", "ANTHROPIC_MODEL")
missing = [k for k in need if not os.environ.get(k)]
for k in need:
    v = os.environ.get(k, "")
    print(f"{k}={'<token>' if k.endswith('_KEY') and v else v or 'MISSING'}")
if missing:
    print("missing contract variables:", missing)
    sys.exit(3)
print(f"seconds to deadline: {int(os.environ['C3_DEADLINE']) - time.time():.0f}")

try:
    req = urllib.request.Request(os.environ["OPENAI_BASE_URL"].rstrip("/") + "/models",
                                 headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"]})
    with urllib.request.urlopen(req, timeout=5) as r:
        print("proxy: ok", json.loads(r.read()).get("data", [{}])[0].get("id"))
except Exception as e:  # stub proxy in plumbing tests: not an error
    print(f"proxy: unreachable ({e.__class__.__name__})")

src = Path(os.environ["C3_WORKSPACE"]) / "src"
files = sorted(p for p in src.rglob("*.py") if "__pycache__" not in p.parts)
if not files:
    print("no Python files under src/")
    sys.exit(0)
target = files[0]
text = target.read_text()
if not text.endswith(MARK):
    target.write_text(text + ("" if text.endswith("\n") else "\n") + MARK)
print(f"edited {target.relative_to(src.parent)}")
