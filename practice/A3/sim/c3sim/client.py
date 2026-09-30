"""Built-in `client` node: plays the scenario workload (runtime.md section 6).

Workload entries (scenario `workload:` list):

    - {at: 1.5, action: submit_job, tenant: A, job_id: j1}          # one action
    - {repeat: 100, at: 2.0, every: 0.25, jitter: 0.1,              # a series; "{i}" in any
       action: submit_job, tenant: B, job_id: "b-{i}"}              # string is 0..repeat-1

Meta keys: at, action, dst (overrides the default target), repeat, every,
jitter. Every other key is an action argument.

An action runs the codebase's action function `ACTIONS[name](client, args)`
(an `async def` returning an outcome dict) or, if there is none, the generic
action: `client.call(name, args)`, i.e. one RPC named after the action to the
target node, with the scenario's `client_retry` policy.
"""

from .errors import ConfigError, NodeCrashed, RpcError, RpcTimeout
from .faults import stream
from .node import Node

META = frozenset({"at", "action", "dst", "repeat", "every", "jitter"})
RESERVED = frozenset({"t", "seq", "kind", "phase", "action", "action_id", "outcome",
                      "attempts", "result", "code", "msg", "node"})
DEFAULT_RETRY = {"timeout": 1.0, "max_attempts": 1, "backoff": 0.0, "retry_on": []}


def _subst(v, i):
    if isinstance(v, str):
        return v.replace("{i}", str(i))
    if isinstance(v, list):
        return [_subst(x, i) for x in v]
    if isinstance(v, dict):
        return {k: _subst(x, i) for k, x in v.items()}
    return v


def expand_workload(workload, seed):
    """Expand repeat entries; returns actions sorted (stably) by time."""
    rng = stream(seed, "workload")
    out = []
    for n, w in enumerate(workload or []):
        if not isinstance(w, dict):
            raise ConfigError(f"workload[{n}]: expected a mapping")
        if "action" not in w:
            raise ConfigError(f"workload[{n}]: missing 'action'")
        if "at" not in w:
            raise ConfigError(f"workload[{n}]: missing 'at'")
        for k in w:
            if k in RESERVED and k not in META:
                raise ConfigError(f"workload[{n}]: argument name {k!r} is reserved")
        rep = int(w.get("repeat", 1))
        every = float(w.get("every", 0.0))
        jitter = float(w.get("jitter", 0.0))
        at0 = float(w["at"])
        for i in range(rep):
            at = at0 + i * every
            if jitter > 0:
                at += rng.uniform(0.0, jitter)
            args = {k: (_subst(v, i) if "repeat" in w else v)
                    for k, v in w.items() if k not in META}
            out.append({"at": at, "action": str(w["action"]), "dst": w.get("dst"),
                        "args": args})
    out.sort(key=lambda a: a["at"])
    for idx, a in enumerate(out):
        a["id"] = idx
    return out


class ActionClient:
    """The handle an action function gets. One per action."""

    def __init__(self, node, action, target, retry):
        self._node = node
        self._ctx = node._ctx
        self.id = action["id"]
        self.action = action["action"]
        self.args = action["args"]
        self.target = action.get("dst") or target
        self.retry = retry
        self.attempts = 0

    @property
    def rng(self):
        return self._ctx.rng

    def now(self):
        return self._ctx.clock.now()

    def sleep(self, seconds):
        return self._ctx.sleep(seconds)

    def spawn(self, coro):
        return self._ctx.spawn(coro)

    def rpc(self, dst, method, payload, timeout):
        self.attempts += 1
        return self._ctx.rpc(dst, method, payload, timeout)

    def send(self, dst, method, payload):
        return self._ctx.send(dst, method, payload)

    def log(self, event, **fields):
        self._ctx.log(event, action_id=self.id, **fields)

    def event(self, phase, **fields):
        """Record an intermediate client trace record, e.g. event("accepted", job_id=...)."""
        rec = {"phase": str(phase), "action_id": self.id, "action": self.action}
        for k, v in fields.items():
            if k in ("t", "seq", "kind", "phase", "action_id", "action"):
                raise ValueError(f"client event field {k!r} is reserved")
            rec[k] = v
        sim = self._ctx._sim
        from .kernel import _copy_json
        sim._emit("client", _copy_json(rec, "client event"))

    async def call(self, method, payload=None, dst=None, timeout=None, max_attempts=None,
                   retry_on=None, backoff=None):
        """RPC with the scenario's client_retry policy. Raises the last RpcTimeout/RpcError."""
        r = self.retry
        timeout = r["timeout"] if timeout is None else timeout
        max_attempts = r["max_attempts"] if max_attempts is None else max_attempts
        retry_on = r["retry_on"] if retry_on is None else retry_on
        backoff = r["backoff"] if backoff is None else backoff
        dst = dst or self.target
        if dst is None:
            raise ConfigError("no client target: set client.target in the scenario or dst on the action")
        payload = {} if payload is None else payload
        last = None
        for attempt in range(max(1, int(max_attempts))):
            if attempt and backoff:
                await self.sleep(backoff * attempt)
            try:
                return await self.rpc(dst, method, payload, timeout)
            except RpcTimeout as e:
                last = e
            except RpcError as e:
                if e.code not in retry_on:
                    raise
                last = e
        raise last


class ClientNode(Node):
    def __init__(self, ctx, sim, actions, target, retry, workload):
        super().__init__(ctx)
        self._sim = sim
        self._actions = actions
        self._target = target
        self._retry = retry
        self._workload = workload
        self.records = {}

    def _schedule_workload(self):
        sim = self._sim
        ns = self._ctx._ns
        for a in self._workload:
            sim._at(a["at"], sim._on_node, (ns, self._ctx._inc, self._start, (a,)))

    def _start(self, a):
        args = a["args"]
        rec = {"id": a["id"], "action": a["action"], "at": a["at"], "args": args,
               "outcome": "unresolved", "t_end": None, "attempts": 0}
        self.records[a["id"]] = rec
        ev = {"phase": "start", "action_id": a["id"], "action": a["action"]}
        ev.update(args)
        from .kernel import _copy_json
        self._sim._emit("client", _copy_json(ev, "action args"))
        self.spawn(self._run(a, rec))

    async def _run(self, a, rec):
        c = ActionClient(self, a, self._target, self._retry)
        fn = self._actions.get(a["action"]) if self._actions else None
        out = {}
        try:
            if fn is None:
                res = await c.call(a["action"], dict(a["args"]))
                out = {"outcome": "ok", "result": res}
            else:
                r = await fn(c, dict(a["args"]))
                if r is None:
                    r = {}
                if not isinstance(r, dict):
                    raise TypeError(f"action {a['action']} returned {type(r).__name__}, not dict")
                out = dict(r)
                out.setdefault("outcome", "ok")
        except RpcTimeout:
            out = {"outcome": "timeout"}
        except RpcError as e:
            out = {"outcome": "error", "code": e.code, "msg": e.msg}
        except NodeCrashed:
            return
        outcome = str(out.pop("outcome"))
        ev = {"phase": "end", "action_id": a["id"], "action": a["action"], "outcome": outcome,
              "attempts": c.attempts}
        ev.update(a["args"])
        for k in ("code", "msg"):
            if k in out:
                ev[k] = out.pop(k)
        if "result" in out:
            ev["result"] = out.pop("result")
        if out:
            ev.setdefault("result", {})
            if isinstance(ev["result"], dict):
                ev["result"] = dict(ev["result"], **out)
        from .kernel import _copy_json
        ev = _copy_json(ev, "action outcome")
        self._sim._emit("client", ev)
        rec.update({"outcome": outcome, "t_end": self._sim.now, "attempts": c.attempts})
        for k in ("code", "msg", "result"):
            if k in ev:
                rec[k] = ev[k]


def install_client(sim):
    sc = sim.scenario
    spec = sim._client_spec
    if spec is not None:
        workload, actions, target, retry = spec
    else:
        workload = sc.get("workload")
        actions = sc.get("_actions")
        target = (sc.get("client") or {}).get("target")
        retry = sc.get("client_retry")
    if target is None:
        target = sc.get("_default_target")
    r = dict(DEFAULT_RETRY)
    r.update(retry or {})
    unknown = set(r) - set(DEFAULT_RETRY)
    if unknown:
        raise ConfigError(f"client_retry: unknown keys {sorted(unknown)}")
    r["timeout"] = float(r["timeout"])
    r["backoff"] = float(r["backoff"])
    r["max_attempts"] = int(r["max_attempts"])
    r["retry_on"] = [str(x) for x in (r["retry_on"] or [])]
    if workload and isinstance(workload[0], dict) and "id" in workload[0] and "args" in workload[0]:
        expanded = workload
    else:
        expanded = expand_workload(workload, sim.seed)
    holder = {}

    def factory(ctx):
        node = ClientNode(ctx, sim, actions or {}, target, r, expanded)
        holder["node"] = node
        return node
    factory._c3sim_builtin = True
    sim.add_node("client", factory)
    ns = sim._nodes["client"]
    sim._boot_client = ns

    class _Handle:
        def _schedule_workload(self):
            holder["node"]._schedule_workload()

        @property
        def records(self):
            return holder["node"].records if "node" in holder else {}
    sim.client_node = _Handle()
