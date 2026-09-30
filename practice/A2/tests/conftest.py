"""Test setup: put the service code and the simulator on sys.path.

Works both in the codebase (runtime at ../../../runtime) and in a case
(simulator at ../sim/c3sim).
"""

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("PYTHONHASHSEED", "0")

_candidates = [os.path.join(ROOT, "sim"),
               os.path.join(os.path.dirname(os.path.dirname(ROOT)), "runtime")]
for _c in _candidates:
    if os.path.isdir(os.path.join(_c, "c3sim")):
        if _c not in sys.path:
            sys.path.insert(0, _c)
        break
_src = os.path.join(ROOT, "src")
if _src not in sys.path:
    sys.path.insert(0, _src)
