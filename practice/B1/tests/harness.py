"""Helpers for shardmr tests.

`FakeCtx` is a synchronous stand-in for a c3sim NodeContext: `spawn` records
coroutines instead of running them, `send` records messages, disk is a dict and
the clock is a settable number. It lets tests drive real node objects' handlers
directly. `replay` runs a scenario through the real simulator.
"""

import importlib.util
import json
import os
import random

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeDisk:
    def __init__(self):
        self.store = {}

    def put(self, key, value):
        self.store[key] = json.dumps(value)

    def get(self, key, default=None):
        s = self.store.get(key)
        return default if s is None else json.loads(s)

    def delete(self, key):
        self.store.pop(key, None)

    def keys(self, prefix=""):
        return sorted(k for k in self.store if k.startswith(prefix))

    def __contains__(self, key):
        return key in self.store


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t


class FakeTask:
    def __init__(self, coro):
        self.coro = coro

    def cancel(self):
        return True


class FakeCtx:
    def __init__(self, name, disk=None, config=None, boot_count=0):
        self.name = name
        self.clock = FakeClock()
        self.rng = random.Random(7)
        self.disk = disk if disk is not None else FakeDisk()
        self.config = dict(config or {})
        self.boot_count = boot_count
        self.logs = []
        self.spawned = []
        self.sent = []

    def log(self, event, **fields):
        self.logs.append(dict(fields, event=event))

    def spawn(self, coro):
        self.spawned.append(coro)
        return FakeTask(coro)

    def send(self, dst, method, payload):
        self.sent.append((dst, method, payload))

    def sleep(self, seconds):
        raise AssertionError("sleep is not available in the unit harness")

    def rpc(self, dst, method, payload, timeout):
        raise AssertionError("rpc is not available in the unit harness")

    def future(self):
        raise AssertionError("future is not available in the unit harness")

    def close_spawned(self):
        for c in self.spawned:
            c.close()
        self.spawned = []

    def events(self, name):
        return [e for e in self.logs if e["event"] == name]


def run_sync(coro):
    """Run a coroutine that never suspends; return its result."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    raise AssertionError("coroutine suspended in the unit harness")


def make_coordinator(disk=None, boot_count=0):
    from shardmr.coordinator import Coordinator
    ctx = FakeCtx("coord", disk=disk, boot_count=boot_count)
    node = Coordinator(ctx)
    run_sync(node.on_start())
    ctx.close_spawned()
    return node, ctx


def call(node, method, payload, src="client"):
    """Invoke a node's handler for `method` synchronously."""
    return run_sync(getattr(node, type(node)._c3sim_handlers[method])(src, payload))


def invariants_module():
    for rel in ("sim/invariants.py", "invariants.py"):
        path = os.path.join(ROOT, rel)
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("shardmr_invariants", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError("invariants.py")


def replay(scenario, seed, faults=None):
    """Run a scenario; returns (events, violations)."""
    from c3sim import runner
    cb, result = runner.run(ROOT, scenario, seed=seed, faults=faults)
    return result.events, runner.check_result(cb, result)


def log_events(events, name):
    return [e for e in events if e["kind"] == "log" and e.get("event") == name]
