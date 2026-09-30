"""Regression-suite setup: same import paths as tests/, in a codebase or a case."""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYTHONHASHSEED", "0")

for cand in (os.path.join(ROOT, "sim"),
             os.path.normpath(os.path.join(ROOT, "..", "..", "runtime"))):
    if os.path.isdir(os.path.join(cand, "c3sim")):
        if cand not in sys.path:
            sys.path.insert(0, cand)
        break

for p in (os.path.join(ROOT, "src"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)
