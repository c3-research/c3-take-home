"""Determinism enforcement (runtime.md section 8).

While service code runs (the kernel wraps every coroutine step, node factory
call and guarded module import in `guarded()`), the forbidden sources of
nondeterminism raise NondeterminismError. Outside service code (kernel, grader,
invariant checkers, tests) everything behaves normally.

Enforcement is by patching module attributes, a `builtins.__import__` wrapper
for forbidden modules, and a `sys.addaudithook` hook for socket, subprocess,
fork/exec and thread-start events. Installed once per process, idempotently.
"""

import builtins
import datetime as _dt
import os
import random
import secrets
import sys
import threading
import time
import uuid

from .errors import NondeterminismError


class _State:
    __slots__ = ("depth", "thread", "violation")

    def __init__(self):
        self.depth = 0
        self.thread = None
        self.violation = None


STATE = _State()
_installed = False

# Original objects, for the runtime's own use.
OriginalRandom = random.Random

FORBIDDEN_IMPORTS = frozenset({
    "asyncio", "multiprocessing", "socket", "subprocess", "select", "selectors",
    "_socket", "_asyncio", "_multiprocessing", "_posixsubprocess", "ssl",
    "socketserver", "concurrent",
})

FORBIDDEN_AUDIT = frozenset({
    "socket.__new__", "socket.bind", "socket.connect", "socket.getaddrinfo",
    "socket.gethostbyname", "socket.gethostbyaddr", "socket.gethostname",
    "socket.getnameinfo", "socket.sendto", "socket.sendmsg",
    "subprocess.Popen", "os.system", "os.exec", "os.fork", "os.forkpty",
    "os.posix_spawn", "os.spawn", "os.startfile", "_thread.start_new_thread",
    "os.kill", "os.killpg",
})


def active():
    s = STATE
    return s.depth > 0 and s.thread == threading.get_ident()


def _violate(what):
    err = NondeterminismError(
        f"{what} is not allowed in service code; use the node's clock, rng, "
        f"sleep, spawn and rpc instead")
    if STATE.violation is None:
        STATE.violation = err
    raise err


class guarded:
    """Context manager marking 'service code is running' on this thread."""

    __slots__ = ()

    def __enter__(self):
        s = STATE
        if s.depth == 0:
            s.thread = threading.get_ident()
        s.depth += 1
        return self

    def __exit__(self, *exc):
        STATE.depth -= 1
        return False


GUARD = guarded()


def _wrap(module, name, label=None, only_without_args=False):
    orig = getattr(module, name, None)
    if orig is None or getattr(orig, "_c3sim_guard", False):
        return
    label = label or f"{module.__name__}.{name}"

    if only_without_args:
        def patched(*args, **kwargs):
            if not args and not kwargs and active():
                _violate(label + "()")
            return orig(*args, **kwargs)
    else:
        def patched(*args, **kwargs):
            if active():
                _violate(label)
            return orig(*args, **kwargs)
    patched._c3sim_guard = True
    patched.__name__ = getattr(orig, "__name__", name)
    patched.__wrapped__ = orig
    setattr(module, name, patched)


class _GuardedDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):
        if active():
            _violate("datetime.now")
        return super().now(tz)

    @classmethod
    def utcnow(cls):
        if active():
            _violate("datetime.utcnow")
        return super().utcnow()

    @classmethod
    def today(cls):
        if active():
            _violate("datetime.today")
        return super().today()


class _GuardedDate(_dt.date):
    @classmethod
    def today(cls):
        if active():
            _violate("date.today")
        return super().today()


_MISSING = object()


class _GuardedRandom(OriginalRandom):
    """random.Random replacement: under the guard, an explicit seed is required."""

    def __init__(self, x=_MISSING):
        if x is _MISSING or x is None:
            if active():
                _violate("random.Random() without a seed")
            x = None
        super().__init__(x)

    def seed(self, a=None, version=2):
        if a is None and active():
            _violate("Random.seed() without a value")
        super().seed(a, version)


def _audit(event, args):
    if event in FORBIDDEN_AUDIT:
        s = STATE
        if s.depth > 0 and s.thread == threading.get_ident():
            _violate(f"audit event {event}")


_orig_import = builtins.__import__


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    s = STATE
    if s.depth > 0 and level == 0 and s.thread == threading.get_ident():
        root = name.partition(".")[0]
        if root in FORBIDDEN_IMPORTS:
            _violate(f"import {name}")
    return _orig_import(name, globals, locals, fromlist, level)


_guarded_import._c3sim_guard = True


def install():
    """Install all guards. Idempotent."""
    global _installed
    if _installed:
        return
    _installed = True
    for n in ("time", "time_ns", "monotonic", "monotonic_ns", "perf_counter",
              "perf_counter_ns", "process_time", "process_time_ns",
              "thread_time", "thread_time_ns", "sleep", "clock_gettime",
              "clock_gettime_ns"):
        _wrap(time, n)
    for n in ("localtime", "gmtime", "ctime", "asctime"):
        _wrap(time, n, only_without_args=True)
    for n in ("random", "uniform", "randint", "randrange", "choice", "choices",
              "shuffle", "sample", "gauss", "normalvariate", "lognormvariate",
              "expovariate", "vonmisesvariate", "gammavariate", "betavariate",
              "paretovariate", "weibullvariate", "triangular", "getrandbits",
              "randbytes", "seed", "binomialvariate", "getstate", "setstate"):
        _wrap(random, n)
    random.Random = _GuardedRandom
    _wrap(random, "SystemRandom")
    _wrap(os, "urandom")
    _wrap(os, "getrandom")
    _wrap(uuid, "uuid1")
    _wrap(uuid, "uuid4")
    for n in ("token_bytes", "token_hex", "token_urlsafe", "randbelow",
              "randbits", "choice", "SystemRandom", "compare_digest"):
        if n != "compare_digest":
            _wrap(secrets, n)
    _dt.datetime = _GuardedDateTime
    _dt.date = _GuardedDate
    _wrap(threading.Thread, "start", label="threading.Thread.start")
    try:
        import _thread
        _wrap(_thread, "start_new_thread")
        _wrap(_thread, "start_joinable_thread")
    except ImportError:  # pragma: no cover
        pass
    for modname in ("select", "multiprocessing", "asyncio", "socket", "subprocess"):
        mod = sys.modules.get(modname)
        if mod is None:
            continue
        for attr in _MODULE_FUNCS.get(modname, ()):
            _wrap(mod, attr)
    builtins.__import__ = _guarded_import
    sys.addaudithook(_audit)


_MODULE_FUNCS = {
    "select": ("select", "poll", "epoll"),
    "asyncio": ("run", "get_event_loop", "new_event_loop", "sleep", "create_task",
                "gather", "wait_for", "ensure_future", "get_running_loop"),
    "multiprocessing": ("Process", "Pool", "Queue", "Manager"),
    "socket": ("socket", "create_connection", "getaddrinfo"),
    "subprocess": ("Popen", "run", "call", "check_call", "check_output"),
}


def reset_violation():
    STATE.violation = None
