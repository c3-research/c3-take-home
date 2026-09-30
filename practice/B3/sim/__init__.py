"""Case simulator package. Run `python -m sim --help` from the case root."""
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
_ROOT = _os.path.dirname(_HERE)
for _p in (_os.path.join(_ROOT, "src"), _HERE):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
