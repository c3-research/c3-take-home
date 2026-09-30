"""Discrete-event kernel: Sim, NodeContext, Task, Future, network, crashes, pauses.

One process, one thread. A heap of (virtual_time, seq, fn, args) drives
everything; ties break by insertion order. Service coroutines are driven with
coro.send/throw and may only await c3sim awaitables (Future, Task, sleep, rpc).
"""

import heapq
import math
import json
import re
from collections import deque

from .determinism import STATE, OriginalRandom, install as _install_guards
from .errors import (Cancelled, ConfigError, NodeCrashed, NondeterminismError,
                     RpcError, RpcTimeout, _NoReply)
from .faults import FaultConfig, derive_seed, glob_match, sample_range, stream

_dumps = json.dumps
_loads = json.loads
_heappush = heapq.heappush
_heappop = heapq.heappop
LIVELOCK_EVENTS_AT_ONE_INSTANT = 200_000

LOG_RESERVED = frozenset({"t", "seq", "kind", "node", "event", "local_t", "node_boot"})


def _copy_json(obj, what):
    try:
        return _loads(_dumps(obj))
    except (TypeError, ValueError) as e:
        raise TypeError(f"{what} must be JSON-serialisable: {e}") from None


# --------------------------------------------------------------------------
# Futures and tasks
# --------------------------------------------------------------------------

class Future:
    """A one-shot result. `await fut` suspends the awaiting task until it is set."""

    __slots__ = ("_sim", "_done", "_result", "_exc", "_waiters")

    def __init__(self, sim):
        self._sim = sim
        self._done = False
        self._result = None
        self._exc = None
        self._waiters = []

    def done(self):
        return self._done

    def result(self):
        if not self._done:
            raise RuntimeError("future not done")
        if self._exc is not None:
            raise self._exc
        return self._result

    def exception(self):
        return self._exc

    def set_result(self, value=None):
        if self._done:
            return False
        self._done = True
        self._result = value
        self._wake()
        return True

    def set_exception(self, exc):
        if self._done:
            return False
        self._done = True
        self._exc = exc
        self._wake()
        return True

    def _wake(self):
        ws = self._waiters
        if ws:
            self._waiters = []
            sim = self._sim
            for task in ws:
                ns = task._ctx._ns
                sim._at(sim.now, sim._on_node, (ns, task._ctx._inc, task._wake, (self,)))

    def __await__(self):
        if not self._done:
            yield self
        if self._exc is not None:
            raise self._exc
        return self._result


class Task:
    """A coroutine running on one node. `await task` returns its result."""

    __slots__ = ("_ctx", "_coro", "_fut", "_waiting", "_cancel_pending", "_id",
                 "_on_done", "__weakref__")

    def __init__(self, ctx, coro, on_done=None):
        self._ctx = ctx
        self._coro = coro
        self._fut = Future(ctx._sim)
        self._waiting = None
        self._cancel_pending = False
        self._on_done = on_done
        ctx._task_seq += 1
        self._id = ctx._task_seq
        ctx._tasks[self._id] = self

    def __await__(self):
        return self._fut.__await__()

    def done(self):
        return self._fut._done

    def result(self):
        return self._fut.result()

    def cancel(self):
        """Raise Cancelled inside the task at its current await point."""
        if self._fut._done:
            return False
        w = self._waiting
        if w is not None:
            try:
                w._waiters.remove(self)
            except ValueError:
                pass
            self._waiting = None
            self._cancel_pending = True
            self._ctx._schedule_step(self)
        else:
            self._cancel_pending = True
        return True

    def _wake(self, fut):
        if self._waiting is not fut:
            return
        self._waiting = None
        self._step()

    def _step(self):
        ctx = self._ctx
        if self._fut._done or not ctx.alive:
            return
        st = STATE
        st.depth += 1
        try:
            if self._cancel_pending:
                self._cancel_pending = False
                y = self._coro.throw(Cancelled())
            else:
                y = self._coro.send(None)
        except StopIteration as e:
            st.depth -= 1
            self._finish(e.value, None)
            return
        except BaseException as e:  # noqa: BLE001 - task errors are captured
            st.depth -= 1
            if isinstance(e, NondeterminismError) and st.violation is None:
                st.violation = e
            self._finish(None, e)
            return
        st.depth -= 1
        if type(y) is not Future:
            err = NondeterminismError(
                f"node {ctx.name}: service code awaited {type(y).__name__!s}, which is "
                f"not a c3sim awaitable (only rpc, sleep, spawned tasks and futures)")
            if st.violation is None:
                st.violation = err
            self._finish(None, err)
            return
        if self._cancel_pending:
            ctx._schedule_step(self)
        elif y._done:
            self._waiting = y
            ctx._sim._at(ctx._sim.now, ctx._sim._on_node,
                         (ctx._ns, ctx._inc, self._wake, (y,)))
        else:
            self._waiting = y
            y._waiters.append(self)

    def _finish(self, result, exc):
        ctx = self._ctx
        ctx._tasks.pop(self._id, None)
        fut = self._fut
        had_waiters = bool(fut._waiters)
        if exc is None:
            fut.set_result(result)
        else:
            fut.set_exception(exc)
            if (not had_waiters and self._on_done is None
                    and not isinstance(exc, (Cancelled, NodeCrashed, NondeterminismError))):
                ctx._unhandled(exc)
        if self._on_done is not None:
            self._on_done(self)

    def _drop(self):
        """Crash: close the coroutine without resuming it. The context is dead."""
        st = STATE
        st.depth += 1
        try:
            self._coro.close()
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, NondeterminismError) and st.violation is None:
                st.violation = e
        finally:
            st.depth -= 1


class Lock:
    """FIFO mutex for tasks on one node. `async with lock: ...`."""

    __slots__ = ("_ctx", "_locked", "_q")

    def __init__(self, ctx):
        self._ctx = ctx
        self._locked = False
        self._q = deque()

    def locked(self):
        return self._locked

    async def acquire(self):
        if not self._locked:
            self._locked = True
            return True
        f = Future(self._ctx._sim)
        self._q.append(f)
        try:
            await f
        except BaseException:
            if f._done and f._exc is None:
                self.release()
            else:
                try:
                    self._q.remove(f)
                except ValueError:
                    pass
            raise
        return True

    def release(self):
        if not self._locked:
            raise RuntimeError("release of an unlocked Lock")
        while self._q:
            f = self._q.popleft()
            if f.set_result(True):
                return
        self._locked = False

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        self.release()
        return False


# --------------------------------------------------------------------------
# Per-node context
# --------------------------------------------------------------------------

class Clock:
    __slots__ = ("_sim", "_ns")

    def __init__(self, sim, ns):
        self._sim = sim
        self._ns = ns

    def now(self):
        ns = self._ns
        return self._sim.now * ns.rate + ns.offset


class Disk:
    """Durable per-node key-value store; survives crashes. Values are JSON."""

    __slots__ = ("_store", "_ctx")

    def __init__(self, store, ctx):
        self._store = store
        self._ctx = ctx

    def put(self, key, value):
        if not self._ctx.alive:
            raise NodeCrashed(self._ctx.name)
        if not isinstance(key, str):
            raise TypeError("disk keys must be str")
        try:
            self._store[key] = _dumps(value)
        except (TypeError, ValueError) as e:
            raise TypeError(f"disk value must be JSON-serialisable: {e}") from None

    def get(self, key, default=None):
        if not self._ctx.alive:
            raise NodeCrashed(self._ctx.name)
        s = self._store.get(key)
        if s is None:
            return default
        return _loads(s)

    def delete(self, key):
        if not self._ctx.alive:
            raise NodeCrashed(self._ctx.name)
        self._store.pop(key, None)

    def keys(self, prefix=""):
        if not self._ctx.alive:
            raise NodeCrashed(self._ctx.name)
        return sorted(k for k in self._store if k.startswith(prefix))

    def __contains__(self, key):
        return key in self._store


class NodeContext:
    """What a node may use. One per incarnation (fresh on every restart)."""

    def __init__(self, sim, ns):
        self._sim = sim
        self._ns = ns
        self._inc = ns.inc
        self.name = ns.name
        self.boot_count = ns.boot_count
        self.clock = Clock(sim, ns)
        self.rng = OriginalRandom(derive_seed(sim.seed, "node", ns.name, ns.boot_count))
        self.alive = True
        self.config = ns.config
        self._tasks = {}
        self._task_seq = 0
        self._pending = {}
        self.disk = Disk(ns.disk, self)

    # --- awaitables ----------------------------------------------------------
    def sleep(self, seconds):
        if not self.alive:
            raise NodeCrashed(self.name)
        sim = self._sim
        fut = Future(sim)
        seconds = float(seconds)
        if seconds < 0:
            seconds = 0.0
        wake = sim.now + seconds / self._ns.rate
        if seconds > 0 and wake <= sim.now:
            # A positive sleep too small to move the float clock (e.g. a timer's leftover 7e-15 s)
            # would wake at the same instant forever; step to the next representable time instead.
            wake = math.nextafter(sim.now, math.inf)
        sim._at(wake, sim._on_node, (self._ns, self._inc, fut.set_result, (None,)))
        return fut

    def spawn(self, coro):
        if not self.alive:
            raise NodeCrashed(self.name)
        if not hasattr(coro, "send") or not hasattr(coro, "throw"):
            raise TypeError("spawn() needs a coroutine object (call the async function)")
        task = Task(self, coro)
        self._schedule_step(task)
        return task

    def future(self):
        if not self.alive:
            raise NodeCrashed(self.name)
        return Future(self._sim)

    def lock(self):
        return Lock(self)

    def rpc(self, dst, method, payload, timeout):
        if not self.alive:
            raise NodeCrashed(self.name)
        sim = self._sim
        fut = Future(sim)
        timeout = float(timeout)
        mid = sim._send(self._ns, str(dst), str(method), payload, "req", None)
        self._pending[mid] = (fut, dst, method, timeout)
        sim._at(sim.now + timeout / self._ns.rate, sim._on_node,
                (self._ns, self._inc, self._rpc_timeout, (mid,)))
        return fut

    def send(self, dst, method, payload):
        if not self.alive:
            raise NodeCrashed(self.name)
        self._sim._send(self._ns, str(dst), str(method), payload, "oneway", None)

    def log(self, event, **fields):
        if not self.alive:
            raise NodeCrashed(self.name)
        self._sim._log(self._ns, str(event), fields)

    # --- internals -------------------------------------------------------------
    def _schedule_step(self, task):
        sim = self._sim
        sim._at(sim.now, sim._on_node, (self._ns, self._inc, task._step, ()))

    def _rpc_timeout(self, mid):
        entry = self._pending.pop(mid, None)
        if entry is None:
            return
        fut, dst, method, timeout = entry
        self._sim._emit("rpc_timeout", {"node": self.name, "dst": dst, "method": method,
                                        "msg_id": mid, "timeout": timeout})
        fut.set_exception(RpcTimeout(dst, method, timeout))

    def _unhandled(self, exc):
        if not self.alive:
            return
        self._sim._log(self._ns, "unhandled_exception", {"error": _exc_text(exc)})


class _NodeState:
    """Kernel-side node record; survives crashes (disk, boot_count, clock)."""

    __slots__ = ("name", "factory", "disk", "boot_count", "inc", "up", "started",
                 "paused", "paused_until", "deferred", "inbox", "rate", "offset",
                 "drift_ppm", "ctx", "instance", "logs", "config", "builtin")

    def __init__(self, name, factory, config, builtin=False):
        self.name = name
        self.factory = factory
        self.disk = {}
        self.boot_count = 0
        self.inc = 0
        self.up = False
        self.started = False
        self.paused = False
        self.paused_until = 0.0
        self.deferred = []
        self.inbox = []
        self.rate = 1.0
        self.offset = 0.0
        self.drift_ppm = 0.0
        self.ctx = None
        self.instance = None
        self.logs = []
        self.config = config
        self.builtin = builtin


# --------------------------------------------------------------------------
# The simulator
# --------------------------------------------------------------------------

class Sim:
    """One simulation run.

    sim = Sim(seed=1, scenario={...})     # scenario keys: see scenario.py
    sim.add_node("router-1", lambda ctx: Router(ctx))
    result = sim.run()                    # -> RunResult
    """

    def __init__(self, seed=0, scenario=None, **overrides):
        _install_guards()
        sc = dict(scenario or {})
        sc.update(overrides)
        self.scenario = sc
        self.seed = int(seed)
        self.name = str(sc.get("name", "adhoc"))
        self.end_at = float(sc.get("end_at", 120.0))
        q = sc.get("quiesce_at")
        self.quiesce_at = float(q) if q is not None else self.end_at
        self.liveness_window = float(sc.get("liveness_window", 30.0))
        faults = sc.get("faults") or {}
        self.faults = faults if isinstance(faults, FaultConfig) else FaultConfig(faults)
        self._explicit_delay = isinstance(faults, dict) and "delay" in (faults.get("network") or {})
        self.expect = sc.get("expect") or {}
        self.now = 0.0
        self._heap = []
        self._seq = 0
        self._mid = 0
        self._nodes = {}
        self._order = []
        self.events = []
        self._link_last = {}
        self._active_parts = []
        self._ran = False
        s = self.seed
        self._r_delay = stream(s, "net.delay")
        self._r_drop = stream(s, "net.drop")
        self._r_dup = stream(s, "net.duplicate")
        self._r_reorder = stream(s, "net.reorder")
        self._r_crash = stream(s, "nodes.crashes")
        self._r_pause = stream(s, "nodes.pauses")
        self._r_clock = stream(s, "nodes.clocks")
        self.gpu_node = None
        self.client_node = None
        gpu = sc.get("gpu")
        if gpu:
            from .gpu import make_gpu
            self.gpu_node = make_gpu(self, gpu if isinstance(gpu, dict) else {})
        self._client_spec = None

    # --- setup ---------------------------------------------------------------
    def add_node(self, name, factory, config=None):
        if self._ran:
            raise ConfigError("add_node after run()")
        name = str(name)
        if name in self._nodes:
            raise ConfigError(f"duplicate node name {name!r}")
        if name in ("client", "gpu", "sim") and not getattr(factory, "_c3sim_builtin", False):
            raise ConfigError(f"node name {name!r} is reserved")
        ns = _NodeState(name, factory, dict(config or {}),
                        builtin=getattr(factory, "_c3sim_builtin", False))
        self._nodes[name] = ns
        self._order.append(name)
        return ns

    @property
    def node_names(self):
        return list(self._order)

    def node_disk(self, name):
        """Test helper: a read-only snapshot of a node's disk."""
        return {k: _loads(v) for k, v in sorted(self._nodes[name].disk.items())}

    def node_instance(self, name):
        """Test helper: the current Node instance (None while crashed)."""
        return self._nodes[name].instance

    def set_client(self, workload=None, actions=None, target=None, retry=None):
        """Configure the built-in client (normally done from the scenario)."""
        self._client_spec = (workload, actions, target, retry)

    # --- scheduling -------------------------------------------------------------
    def _at(self, t, fn, args):
        self._seq += 1
        _heappush(self._heap, (t, self._seq, fn, args))

    def _on_node(self, ns, inc, fn, args):
        if ns.inc != inc or not ns.up:
            return
        if ns.paused:
            ns.deferred.append((fn, args))
            return
        fn(*args)

    # --- trace ------------------------------------------------------------------
    def _emit(self, kind, fields):
        if STATE.violation is not None:
            raise STATE.violation
        ev = fields
        ev["t"] = self.now
        ev["seq"] = len(self.events)
        ev["kind"] = kind
        self.events.append(ev)
        return ev

    def _fault(self, fault, **fields):
        fields["fault"] = fault
        return self._emit("fault", fields)

    def _log(self, ns, event, fields, copy=True):
        if fields:
            for k in fields:
                if k in LOG_RESERVED:
                    raise ValueError(f"log field name {k!r} is reserved")
            if copy:
                fields = _copy_json(fields, "log fields")
        local = self.now * ns.rate + ns.offset
        ev = dict(fields) if fields else {}
        ev["node"] = ns.name
        ev["event"] = event
        ev["local_t"] = local
        ev["node_boot"] = ns.boot_count
        self._emit("log", ev)
        parts = [f"t={local:.6f} node={ns.name} event={event}"]
        for k, v in fields.items():
            parts.append(f"{k}={_fmt(v)}")
        ns.logs.append(" ".join(parts))

    def _syslog(self, ns, event, **fields):
        """Runtime lifecycle line in the node's log file only (not a trace log record)."""
        local = self.now * ns.rate + ns.offset
        parts = [f"t={local:.6f} node={ns.name} event={event}"]
        for k, v in fields.items():
            parts.append(f"{k}={_fmt(v)}")
        ns.logs.append(" ".join(parts))

    # --- network ----------------------------------------------------------------
    def _partitioned(self, a, b):
        for groups in self._active_parts:
            ga = groups.get(a)
            if ga is not None:
                gb = groups.get(b)
                if gb is not None and gb != ga:
                    return True
        return False

    def _send(self, src_ns, dst, method, body, typ, rpc_id):
        if body is None:
            body = {}
        if typ != "resp" and not isinstance(body, dict):
            raise TypeError(f"payload for {method} must be a dict, got {type(body).__name__}")
        try:
            s = _dumps(body)
        except (TypeError, ValueError) as e:
            raise TypeError(f"payload for {method} must be JSON-serialisable: {e}") from None
        self._mid += 1
        mid = self._mid
        src = src_ns.name
        ev = {"src": src, "dst": dst, "method": method, "msg_id": mid, "type": typ,
              "payload": _loads(s)}
        if rpc_id is not None:
            ev["rpc_id"] = rpc_id
        self._emit("msg_send", ev)
        if dst not in self._nodes:
            self._emit("msg_drop", {"src": src, "dst": dst, "method": method, "msg_id": mid,
                                    "type": typ, "reason": "unknown_node"})
            return mid
        now = self.now
        f = self.faults
        on = now < self.quiesce_at
        if self._active_parts and self._partitioned(src, dst):
            self._emit("msg_drop", {"src": src, "dst": dst, "method": method, "msg_id": mid,
                                    "type": typ, "reason": "partition"})
            return mid
        if on and f.drop > 0.0 and self._r_drop.random() < f.drop:
            self._fault("drop", src=src, dst=dst, msg_id=mid, method=method)
            self._emit("msg_drop", {"src": src, "dst": dst, "method": method, "msg_id": mid,
                                    "type": typ, "reason": "loss"})
            return mid
        delay = f.delay.sample(self._r_delay)
        if self._explicit_delay:
            self._fault("delay", src=src, dst=dst, msg_id=mid, delay=delay)
        t = now + delay
        if on and f.reorder > 0.0 and self._r_reorder.random() < f.reorder:
            extra = self._r_reorder.uniform(0.0, f.reorder_window)
            self._fault("reorder", src=src, dst=dst, msg_id=mid, extra_delay=extra)
            t += extra
        else:
            key = (src, dst)
            last = self._link_last.get(key, 0.0)
            if t < last:
                t = last
            self._link_last[key] = t
        msg = (mid, src, dst, method, typ, rpc_id, s)
        self._at(t, self._deliver, (msg,))
        if on and f.duplicate > 0.0 and self._r_dup.random() < f.duplicate:
            t2 = t + f.delay.sample(self._r_dup)
            self._fault("duplicate", src=src, dst=dst, msg_id=mid, delay=t2 - now)
            self._at(t2, self._deliver, (msg,))
        return mid

    def _deliver(self, msg):
        mid, src, dst, method, typ, rpc_id, s = msg
        ns = self._nodes[dst]
        if not ns.up:
            self._emit("msg_drop", {"src": src, "dst": dst, "method": method, "msg_id": mid,
                                    "type": typ, "reason": "node_down"})
            return
        if ns.paused:
            ns.deferred.append((self._deliver, (msg,)))
            return
        if self._active_parts and self._partitioned(src, dst):
            self._emit("msg_drop", {"src": src, "dst": dst, "method": method, "msg_id": mid,
                                    "type": typ, "reason": "partition"})
            return
        if typ != "resp" and not ns.started:
            ns.inbox.append(msg)
            return
        self._emit("msg_recv", {"src": src, "dst": dst, "method": method, "msg_id": mid,
                                "type": typ})
        ctx = ns.ctx
        if typ == "resp":
            entry = ctx._pending.pop(rpc_id, None)
            if entry is None:
                return
            body = _loads(s)
            fut = entry[0]
            if "err" in body:
                code, m = body["err"]
                fut.set_exception(RpcError(code, m))
            else:
                fut.set_result(body.get("ok"))
            return
        inst = ns.instance
        attr = type(inst)._c3sim_handlers.get(method)
        if attr is None:
            if typ == "req":
                self._send(ns, src, method, {"err": ["NO_SUCH_METHOD", f"{dst} has no handler {method!r}"]},
                           "resp", mid)
            return
        payload = _loads(s)
        fn = getattr(inst, attr)
        task = Task(ctx, _serve(fn, src, payload))
        if typ == "req":
            task._on_done = _make_replier(self, ns, ctx, src, method, mid)
        ctx._schedule_step(task)

    # --- node lifecycle ----------------------------------------------------------
    def _boot(self, ns):
        ns.up = True
        ns.started = False
        ns.paused = False
        ctx = NodeContext(self, ns)
        ns.ctx = ctx
        if ns.boot_count > 0:
            self._emit("restart", {"node": ns.name, "boot": ns.boot_count})
            self._fault("restart", node=ns.name, boot=ns.boot_count)
            self._syslog(ns, "node_restart", boot=ns.boot_count)
        st = STATE
        st.depth += 1
        try:
            inst = ns.factory(ctx)
        except NondeterminismError as e:
            if st.violation is None:
                st.violation = e
            raise
        finally:
            st.depth -= 1
        if not hasattr(type(inst), "_c3sim_handlers"):
            raise ConfigError(f"factory for {ns.name} must return a c3sim.Node, got {type(inst).__name__}")
        ns.instance = inst

        def started(task, ns=ns, ctx=ctx):
            if not ctx.alive:
                return
            exc = task._fut._exc
            if exc is not None and not isinstance(exc, (Cancelled, NodeCrashed)):
                ctx._unhandled(exc)
            ns.started = True
            inbox, ns.inbox = ns.inbox, []
            for m in inbox:
                self._at(self.now, self._deliver, (m,))

        task = Task(ctx, _call_on_start(inst), on_done=started)
        ctx._schedule_step(task)

    def _crash(self, ns, restart_after, source):
        if not ns.up:
            return
        self._emit("crash", {"node": ns.name, "boot": ns.boot_count})
        self._fault("crash", node=ns.name, boot=ns.boot_count, restart_after=restart_after,
                    source=source)
        self._syslog(ns, "node_crash", boot=ns.boot_count)
        ctx = ns.ctx
        ctx.alive = False
        ns.up = False
        ns.started = False
        ns.paused = False
        ns.inc += 1
        lost = list(ns.inbox)
        for fn, args in ns.deferred:
            if fn == self._deliver:
                lost.append(args[0])
        ns.inbox = []
        ns.deferred = []
        for m in lost:
            self._emit("msg_drop", {"src": m[1], "dst": m[2], "method": m[3], "msg_id": m[0],
                                    "type": m[4], "reason": "node_down"})
        tasks = list(ctx._tasks.values())
        ctx._tasks.clear()
        ctx._pending.clear()
        for t in tasks:
            t._drop()
        ns.instance = None
        ns.ctx = None
        if restart_after is not None:
            self._at(self.now + restart_after, self._restart, (ns,))

    def _restart(self, ns):
        if ns.up:
            return
        ns.boot_count += 1
        self._boot(ns)

    def _pause(self, ns, duration):
        if not ns.up:
            return
        until = self.now + duration
        self._fault("pause", node=ns.name, duration=duration, until=until)
        self._syslog(ns, "node_pause", duration=duration)
        if ns.paused:
            if until > ns.paused_until:
                ns.paused_until = until
        else:
            ns.paused = True
            ns.paused_until = until
        self._at(until, self._resume, (ns, ns.inc))

    def _resume(self, ns, inc):
        if ns.inc != inc or not ns.paused or self.now < ns.paused_until:
            return
        ns.paused = False
        self._syslog(ns, "node_resume")
        d, ns.deferred = ns.deferred, []
        for fn, args in d:
            self._at(self.now, self._on_node, (ns, ns.inc, fn, args))

    def _partition_start(self, idx, groups):
        self._fault("partition_start", partition=idx,
                    groups=[sorted(n for n, g in groups.items() if g == i)
                            for i in range(max(groups.values(), default=-1) + 1)])
        self._active_parts.append(groups)

    def _partition_end(self, idx, groups):
        self._fault("partition_end", partition=idx)
        try:
            self._active_parts.remove(groups)
        except ValueError:
            pass

    # --- faults schedule -------------------------------------------------------
    def _schedule_faults(self):
        f = self.faults
        names = self._order
        for c in f.clocks:
            for n in names:
                if glob_match(c["node"], n):
                    ns = self._nodes[n]
                    drift = sample_range(c["drift_ppm"], self._r_clock)
                    off = sample_range(c["offset"], self._r_clock)
                    ns.drift_ppm = drift
                    ns.rate = 1.0 + drift * 1e-6
                    ns.offset = off
                    if ns.rate <= 0:
                        raise ConfigError(f"clock drift for {n} makes the clock run backwards")
        for n in names:
            ns = self._nodes[n]
            if ns.drift_ppm != 0.0 or ns.offset != 0.0:
                self._fault("clock_skew", node=n, drift_ppm=ns.drift_ppm, offset=ns.offset)
        for i, p in enumerate(f.partitions):
            groups = {}
            for gi, pats in enumerate(p["groups"]):
                for n in names:
                    if n not in groups and any(glob_match(pat, n) for pat in pats):
                        groups[n] = gi
            self._at(p["at"], self._partition_start, (i, groups))
            self._at(p["until"], self._partition_end, (i, groups))
        r = self._r_crash
        for c in f.crashes:
            targets = [n for n in names if glob_match(c["node"], n)]
            if c["at"] is not None:
                at = sample_range(c["at"], r)
                for n in targets:
                    ra = sample_range(c["restart_after"], r)
                    self._at(at, self._crash, (self._nodes[n], ra, "scheduled"))
            if c["rate_per_hour"] > 0:
                lam = c["rate_per_hour"] / 3600.0
                for n in targets:
                    t = 0.0
                    while True:
                        t += r.expovariate(lam)
                        if t >= self.quiesce_at:
                            break
                        ra = sample_range(c["restart_after"], r)
                        self._at(t, self._crash, (self._nodes[n], ra, "random"))
        r = self._r_pause
        for c in f.pauses:
            targets = [n for n in names if glob_match(c["node"], n)]
            if c["at"] is not None:
                at = sample_range(c["at"], r)
                for n in targets:
                    self._at(at, self._pause, (self._nodes[n], sample_range(c["duration"], r)))
            if c["rate_per_hour"] > 0:
                lam = c["rate_per_hour"] / 3600.0
                for n in targets:
                    t = 0.0
                    while True:
                        t += r.expovariate(lam)
                        if t >= self.quiesce_at:
                            break
                        self._at(t, self._pause, (self._nodes[n], sample_range(c["duration"], r)))

    # --- run ---------------------------------------------------------------------
    def run(self):
        """Run to end_at (or until nothing is scheduled). Returns a RunResult."""
        if self._ran:
            raise RuntimeError("a Sim runs once; build a new one")
        from .client import install_client
        from .trace import RunResult
        st = STATE
        if st.depth == 0:
            import threading
            st.thread = threading.get_ident()
        st.violation = None
        install_client(self)
        self._ran = True
        gpu_cap = self.gpu_node.capacity if self.gpu_node is not None else None
        self._emit("log", {"node": "sim", "event": "scenario", "local_t": 0.0, "node_boot": 0,
                           "scenario": self.name, "seed": self.seed, "end_at": self.end_at,
                           "quiesce_at": self.quiesce_at,
                           "liveness_window": self.liveness_window,
                           "gpu_capacity": gpu_cap, "nodes": list(self._order),
                           "expect": _copy_json(self.expect, "expect")})
        self._schedule_faults()
        for n in self._order:
            self._boot(self._nodes[n])
        if self.client_node is not None:
            self.client_node._schedule_workload()
        heap = self._heap
        end = self.end_at
        error = None
        # Livelock guard: service code that re-schedules itself without letting virtual time
        # advance would otherwise spin until the wall-clock replay timeout. After this many
        # events at one instant the run stops with a `livelock_detected` record; unresolved
        # client actions then fail I2 quickly and deterministically.
        same_t, same_n = None, 0
        try:
            while heap:
                item = heap[0]
                if item[0] > end:
                    break
                _heappop(heap)
                self.now = item[0]
                if item[0] == same_t:
                    same_n += 1
                    if same_n > LIVELOCK_EVENTS_AT_ONE_INSTANT:
                        self._emit("log", {"node": "sim", "event": "livelock_detected",
                                           "local_t": self.now, "node_boot": 0,
                                           "events_at_instant": same_n})
                        break
                else:
                    same_t, same_n = item[0], 0
                item[2](*item[3])
                if st.violation is not None:
                    raise st.violation
        except NondeterminismError as e:
            error = e
        if error is None:
            self.now = max(self.now, end) if heap else self.now
        return RunResult(self, error)


async def _call_on_start(inst):
    r = inst.on_start()
    if hasattr(r, "__await__"):
        r = await r
    return r


async def _serve(fn, src, payload):
    r = fn(src, payload)
    if hasattr(r, "__await__"):
        r = await r
    return r


def _make_replier(sim, ns, ctx, src, method, mid):
    def reply(task):
        if not ctx.alive:
            return
        exc = task._fut._exc
        if exc is None:
            r = task._fut._result
            if r is None:
                r = {}
            if not isinstance(r, dict):
                ctx._unhandled(TypeError(f"handler {method} returned {type(r).__name__}, not dict"))
                body = {"err": ["INTERNAL", f"handler {method} returned non-dict"]}
            else:
                body = {"ok": r}
        elif isinstance(exc, RpcError):
            body = {"err": [exc.code, exc.msg]}
        elif isinstance(exc, (_NoReply, Cancelled, NodeCrashed, NondeterminismError)):
            return
        else:
            ctx._unhandled(exc)
            body = {"err": ["INTERNAL", _exc_text(exc)]}
        try:
            sim._send(ns, src, method, body, "resp", mid)
        except TypeError as e:
            ctx._unhandled(e)
            sim._send(ns, src, method, {"err": ["INTERNAL", str(e)]}, "resp", mid)
    return reply


_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")


def _exc_text(exc):
    """Exception text with memory addresses scrubbed (they differ between processes)."""
    return _ADDR.sub(" at 0x?", f"{type(exc).__name__}: {exc}")


def _fmt(v):
    if isinstance(v, str):
        if v and not any(c in v for c in ' ="\n\t'):
            return v
        return _dumps(v)
    if isinstance(v, float):
        return repr(v)
    return _dumps(v, separators=(",", ":"), sort_keys=True)
