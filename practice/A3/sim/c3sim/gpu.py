"""The built-in simulated GPU service (runtime.md section 5; corpus/platform/gpu-api-v3.md).

Enabled by a scenario `gpu:` block: `gpu: {capacity: 8, release_delay: <dist>}`.
Fault knobs come from the scenario's `faults.gpu` block.
"""

import hashlib
import json

from .errors import RpcError, _NoReply
from .faults import Dist, stream
from .node import Node, handler

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


class _Op:
    __slots__ = ("op_id", "tenant", "units", "duration", "payload", "state", "result",
                 "released", "releasing", "releaser", "task", "fail", "src")

    def __init__(self, op_id, tenant, units, duration, payload, src):
        self.op_id = op_id
        self.tenant = tenant
        self.units = units
        self.duration = duration
        self.payload = payload
        self.src = src
        self.state = "PENDING"
        self.result = None
        self.released = False
        self.releasing = False
        self.releaser = None
        self.task = None
        self.fail = False


class GpuNode(Node):
    """Platform GPU. State lives in the kernel-side GpuState, so it never resets."""

    def __init__(self, ctx, state):
        super().__init__(ctx)
        self._g = state

    @handler("submit")
    async def submit(self, src, p):
        g = self._g
        op_id = p.get("op_id")
        if not isinstance(op_id, str) or not op_id:
            raise RpcError("INVALID", "op_id must be a non-empty string")
        op = g.ops.get(op_id)
        if op is not None:
            r = {"accepted": True, "state": op.state}
            if op.state == "SUCCEEDED":
                r["result"] = op.result
            return r
        units = p.get("units")
        if not isinstance(units, int) or isinstance(units, bool) or units < 1:
            raise RpcError("INVALID", "units must be an integer >= 1")
        duration = p.get("duration", 0.0)
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
            raise RpcError("INVALID", "duration must be a number >= 0")
        payload = p.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise RpcError("INVALID", "payload must be an object")
        tenant = str(p.get("tenant", ""))
        if g.committed + units > g.capacity:
            raise RpcError("CAPACITY", f"{g.committed}+{units} units > capacity {g.capacity}")
        op = _Op(op_id, tenant, units, float(duration), payload, src)
        g.ops[op_id] = op
        g.committed += units
        sim = g.sim
        on = sim.now < sim.quiesce_at
        f = sim.faults
        op.fail = on and f.gpu_fail > 0 and g.r_fail.random() < f.gpu_fail
        g.effect("accepted", op, src=src, payload=payload)
        op.task = self.spawn(self._run(op))
        if on and f.gpu_lost_reply > 0 and g.r_lost.random() < f.gpu_lost_reply:
            sim._fault("gpu_lost_reply", node="gpu", op_id=op_id, src=src)
            raise _NoReply()
        if on and f.gpu_arf > 0 and g.r_arf.random() < f.gpu_arf:
            sim._fault("gpu_accepted_reported_failed", node="gpu", op_id=op_id, src=src)
            raise RpcError("UNAVAILABLE", "service busy, try again")
        return {"accepted": True}

    async def _run(self, op):
        g = self._g
        lat = g.sim.faults.gpu_latency.sample(g.r_latency)
        if lat > 0:
            await self.sleep(lat)
        if op.state != "PENDING":
            return
        op.state = "RUNNING"
        g.effect("started", op)
        if op.duration > 0:
            await self.sleep(op.duration)
        if op.state != "RUNNING":
            return
        if op.fail:
            op.state = "FAILED"
            g.sim._fault("gpu_fail", node="gpu", op_id=op.op_id)
        else:
            op.state = "SUCCEEDED"
            op.result = _result(op)
        g.effect("finished", op)

    @handler("status")
    async def status(self, src, p):
        op = self._g.ops.get(p.get("op_id"))
        if op is None:
            raise RpcError("NOT_FOUND", "no such operation")
        r = {"state": op.state, "released": op.released}
        if op.state == "SUCCEEDED":
            r["result"] = op.result
        return r

    @handler("cancel")
    async def cancel(self, src, p):
        g = self._g
        op = g.ops.get(p.get("op_id"))
        if op is None:
            raise RpcError("NOT_FOUND", "no such operation")
        if op.state in ("PENDING", "RUNNING"):
            op.state = "CANCELLED"
            if op.task is not None:
                op.task.cancel()
            g.effect("cancelled", op, src=src)
        return {"ok": True}

    @handler("release")
    async def release(self, src, p):
        g = self._g
        op = g.ops.get(p.get("op_id"))
        if op is None:
            raise RpcError("NOT_FOUND", "no such operation")
        if op.state not in TERMINAL:
            raise RpcError("NOT_TERMINAL", f"operation is {op.state}")
        if op.releasing or op.released:
            return {"ok": True}
        op.releasing = True
        op.releaser = src
        g.effect("release_requested", op, src=src)
        self.spawn(self._free(op))
        return {"ok": True}

    async def _free(self, op):
        g = self._g
        d = g.release_delay.sample(g.r_release)
        if d > 0:
            await self.sleep(d)
        g.committed -= op.units
        op.released = True
        g.effect("release_complete", op)
        self.send(op.releaser, "release_complete", {"op_id": op.op_id})


class GpuState:
    def __init__(self, sim, spec):
        spec = dict(spec)
        self.sim = sim
        self.capacity = int(spec.pop("capacity", 8))
        self.release_delay = Dist(spec.pop("release_delay", {"dist": "uniform", "low": 0.05,
                                                               "high": 0.3}), "gpu.release_delay")
        if spec:
            from .errors import ConfigError
            raise ConfigError(f"gpu: unknown keys {sorted(spec)} (capacity|release_delay)")
        self.ops = {}
        self.committed = 0
        s = sim.seed
        self.r_fail = stream(s, "gpu.fail")
        self.r_lost = stream(s, "gpu.lost_reply")
        self.r_arf = stream(s, "gpu.accepted_reported_failed")
        self.r_latency = stream(s, "gpu.latency")
        self.r_release = stream(s, "gpu.release_delay")

    def effect(self, effect, op, **extra):
        ev = {"effect": effect, "op_id": op.op_id, "tenant": op.tenant, "units": op.units,
              "committed": self.committed, "capacity": self.capacity, "state": op.state}
        ev.update(extra)
        self.sim._emit("gpu_effect", ev)
        ns = self.sim._nodes["gpu"]
        self.sim._syslog(ns, "gpu_" + effect, op_id=op.op_id, tenant=op.tenant,
                         units=op.units, state=op.state, committed=self.committed)


def _result(op):
    h = hashlib.sha256((op.op_id + "\x00" + json.dumps(op.payload, sort_keys=True))
                       .encode()).hexdigest()[:16]
    return {"op_id": op.op_id, "digest": h, "payload": op.payload}


def make_gpu(sim, spec):
    state = GpuState(sim, spec)

    def factory(ctx):
        return GpuNode(ctx, state)
    factory._c3sim_builtin = True
    sim.add_node("gpu", factory)
    return state
