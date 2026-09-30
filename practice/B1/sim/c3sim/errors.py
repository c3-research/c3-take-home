"""Exceptions raised by the c3sim runtime."""


class RpcTimeout(Exception):
    """No reply arrived within the caller's local-clock timeout. Outcome unknown."""

    def __init__(self, dst="", method="", timeout=0.0):
        super().__init__(f"rpc {method} to {dst} timed out after {timeout!r}s")
        self.dst = dst
        self.method = method
        self.timeout = timeout


class RpcError(Exception):
    """Raised by a handler to return an error reply; re-raised at the caller."""

    def __init__(self, code, msg=""):
        super().__init__(f"{code}: {msg}" if msg else str(code))
        self.code = str(code)
        self.msg = str(msg)


class NodeCrashed(Exception):
    """The calling node crashed; its context is dead and can do nothing more."""


class Cancelled(BaseException):
    """Raised inside a task at its current await point by Task.cancel().

    Subclasses BaseException (like asyncio.CancelledError) so `except Exception`
    in service code does not swallow it by accident.
    """


class NondeterminismError(BaseException):
    """Service code used a forbidden source of time, randomness, threads or I/O.

    Subclasses BaseException so a broad `except Exception` cannot hide it; the
    kernel also records the first violation and aborts the run regardless.
    """


class ConfigError(ValueError):
    """Invalid scenario, fault config or plugin."""


class _NoReply(BaseException):
    """Internal: a handler that must not send any reply (GPU lost_reply fault)."""
