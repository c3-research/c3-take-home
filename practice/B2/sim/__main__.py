"""Case entry point: `python -m sim ...` from the case root (clarifications.md C1).

Layout: case/sim/{__init__.py,__main__.py,c3sim/,invariants.py,scenarios.py?};
case/src/ holds the service code. Both case/sim/ and case/src/ go on sys.path.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (os.path.join(_ROOT, "src"), _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from c3sim.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(root=_ROOT, prog="python -m sim"))
