"""c3sim: deterministic discrete-event simulator for C3 service codebases.

See runtime/README.md and RULES.md.
"""

import sys as _sys

from .determinism import install as _install

_install()

from .errors import (Cancelled, ConfigError, NodeCrashed, NondeterminismError,  # noqa: E402
                     RpcError, RpcTimeout)
from .kernel import Future, Lock, NodeContext, Sim, Task  # noqa: E402
from .node import Node, handler  # noqa: E402

__all__ = ["Sim", "Node", "handler", "RpcTimeout", "RpcError", "NodeCrashed",
           "NondeterminismError", "Cancelled", "ConfigError", "Future", "Task", "Lock",
           "NodeContext"]
__version__ = "1.0.0"

# Service code always does `import c3sim`; make that resolve to this copy even
# if it was imported under another name.
if __name__ != "c3sim":
    _sys.modules.setdefault("c3sim", _sys.modules[__name__])
