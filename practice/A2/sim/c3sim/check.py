"""Trace checks: `expect` blocks and reusable common invariants (I0, I1, I2, I3).

Violation = {"invariant": str, "t": float|None, "detail": str}.

Built-in `expect` keys (codebases add more via the scenario loader's EXPECT):

    all_actions_resolved_by: T    every client action has an end record at t <= T
    all_jobs_complete_by: T       ... and every outcome is "ok"
    outcomes_max: {timeout: 0}    at most N end records with that outcome
    outcomes_min: {ok: 10}        at least N
    log_events_max: {event: N}    at most N log records with that event (N=0 forbids it)
    log_events_min: {event: N}
    messages_max: N               at most N msg_send records
    no_unhandled_exceptions: true no unhandled_exception log records
    rpc_errors_max: [{src, dst, method, code, max}]
                                  at most `max` RPC error replies (msg_send type=resp with
                                  payload.err[0] == code) for requests src->dst:method. src/dst/
                                  method accept fnmatch globs; omitted fields match anything.
                                  Trace-based: service code can't hide these by not logging.
    rpc_requests_max: [{src, dst, method, max}]
                                  at most `max` requests (msg_send type=req, or type=oneway for
                                  one-way sends) src->dst:method in total (globs as above).
    rpc_requests_per_group_max: [{src, dst, method, group_by, max}]
                                  for msg_send type=req src->dst:method, group by
                                  payload[group_by] (a dotted path, e.g. "key" or "job.id"). No
                                  group may exceed `max` requests (e.g. a documented retry bound).
"""
from fnmatch import fnmatchcase

from .trace import scenario_record


def V(inv, t, detail):
    return {"invariant": inv, "t": t, "detail": detail}


def _client_ends(events):
    starts, ends = {}, {}
    for e in events:
        if e["kind"] == "client":
            if e.get("phase") == "start":
                starts[e["action_id"]] = e
            elif e.get("phase") == "end":
                ends.setdefault(e["action_id"], e)
    return starts, ends


def _x_all_resolved(value, events, require_ok=False):
    starts, ends = _client_ends(events)
    out = []
    for aid in sorted(starts):
        s = starts[aid]
        e = ends.get(aid)
        if e is None:
            out.append(V("expect", None, f"action {aid} ({s['action']}) never resolved"))
        elif e["t"] > float(value):
            out.append(V("expect", e["t"], f"action {aid} ({s['action']}) resolved at "
                                           f"{e['t']:.3f} > {value}"))
        elif require_ok and e.get("outcome") != "ok":
            out.append(V("expect", e["t"], f"action {aid} ({s['action']}) outcome "
                                           f"{e.get('outcome')}"))
    return out


def _count_outcomes(events):
    _, ends = _client_ends(events)
    c = {}
    for e in ends.values():
        c[e.get("outcome")] = c.get(e.get("outcome"), 0) + 1
    return c


def _x_outcomes(value, events, mode):
    c = _count_outcomes(events)
    out = []
    for k, n in sorted(dict(value).items()):
        have = c.get(k, 0)
        if (mode == "max" and have > n) or (mode == "min" and have < n):
            out.append(V("expect", None, f"outcome {k}: {have} ({mode} {n})"))
    return out


def _x_logs(value, events, mode):
    c = {}
    for e in events:
        if e["kind"] == "log":
            c[e["event"]] = c.get(e["event"], 0) + 1
    out = []
    for k, n in sorted(dict(value).items()):
        have = c.get(k, 0)
        if (mode == "max" and have > n) or (mode == "min" and have < n):
            out.append(V("expect", None, f"log event {k}: {have} ({mode} {n})"))
    return out


def _m(pat, val):
    return pat is None or fnmatchcase(str(val), str(pat))


def _dig(payload, path):
    cur = payload
    for part in str(path).split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur if isinstance(cur, (str, int, float, bool)) else repr(cur)


def _x_rpc_errors(value, events):
    rules = value if isinstance(value, list) else [value]
    reqs = {}
    for e in events:
        if e["kind"] == "msg_send" and e.get("type") == "req":
            reqs[e["msg_id"]] = e
    out = []
    for r in rules:
        n = 0
        for e in events:
            if e["kind"] != "msg_send" or e.get("type") != "resp":
                continue
            err = (e.get("payload") or {}).get("err")
            if not err:
                continue
            q = reqs.get(e.get("rpc_id"))
            if q is None:
                continue
            if (_m(r.get("src"), q["src"]) and _m(r.get("dst"), q["dst"])
                    and _m(r.get("method"), q["method"]) and _m(r.get("code"), err[0])):
                n += 1
        if n > int(r.get("max", 0)):
            out.append(V("expect", None, f"rpc errors {r}: {n} > {r.get('max', 0)}"))
    return out


def _x_rpc_per_group(value, events):
    rules = value if isinstance(value, list) else [value]
    out = []
    for r in rules:
        c = {}
        for e in events:
            if (e["kind"] == "msg_send" and e.get("type") == "req" and _m(r.get("src"), e["src"])
                    and _m(r.get("dst"), e["dst"]) and _m(r.get("method"), e["method"])):
                k = _dig(e.get("payload") or {}, r["group_by"])
                if k is not None:
                    c[k] = c.get(k, 0) + 1
        worst = max(c.items(), key=lambda kv: kv[1], default=(None, 0))
        if worst[1] > int(r["max"]):
            out.append(V("expect", None, f"rpc requests per {r['group_by']}={worst[0]!r}: "
                                         f"{worst[1]} > {r['max']} ({r})"))
    return out


def _x_rpc_total(value, events):
    rules = value if isinstance(value, list) else [value]
    out = []
    for r in rules:
        n = sum(1 for e in events if e["kind"] == "msg_send" and e.get("type") in ("req", "oneway")
                and _m(r.get("src"), e["src"]) and _m(r.get("dst"), e["dst"])
                and _m(r.get("method"), e["method"]))
        if n > int(r["max"]):
            out.append(V("expect", None, f"rpc requests {r}: {n} > {r['max']}"))
    return out


BUILTIN_EXPECT = {
    "rpc_requests_max": lambda v, ev: _x_rpc_total(v, ev),
    "rpc_errors_max": lambda v, ev: _x_rpc_errors(v, ev),
    "rpc_requests_per_group_max": lambda v, ev: _x_rpc_per_group(v, ev),
    "all_actions_resolved_by": lambda v, ev: _x_all_resolved(v, ev),
    "all_jobs_complete_by": lambda v, ev: _x_all_resolved(v, ev, require_ok=True),
    "outcomes_max": lambda v, ev: _x_outcomes(v, ev, "max"),
    "outcomes_min": lambda v, ev: _x_outcomes(v, ev, "min"),
    "log_events_max": lambda v, ev: _x_logs(v, ev, "max"),
    "log_events_min": lambda v, ev: _x_logs(v, ev, "min"),
    "messages_max": lambda v, ev: ([] if sum(1 for e in ev if e["kind"] == "msg_send") <= int(v)
                                   else [V("expect", None, f"messages sent > {v}")]),
    "no_unhandled_exceptions": lambda v, ev: ([] if not v else
                                              [V("expect", e["t"], f"{e['node']}: {e.get('error')}")
                                               for e in ev if e["kind"] == "log"
                                               and e.get("event") == "unhandled_exception"]),
}


def evaluate_expect(expect, events, extra=None):
    checks = dict(BUILTIN_EXPECT)
    checks.update(extra or {})
    out = []
    for key in sorted(expect or {}):
        fn = checks.get(key)
        if fn is None:
            out.append(V("expect", None, f"unknown expect key {key!r}"))
            continue
        out.extend(fn(expect[key], events) or [])
    return out


# --- common invariants (codebases may call these from invariants.py) ---------------

def check_i0(events):
    """I0: every gpu_effect references an op_id that some accepted submit created."""
    seen, out = set(), []
    for e in events:
        if e["kind"] == "gpu_effect":
            if e["effect"] == "accepted":
                seen.add(e["op_id"])
            elif e["op_id"] not in seen:
                out.append(V("I0", e["t"], f"effect {e['effect']} for unknown op {e['op_id']}"))
    return out


def check_i1(events):
    """I1: committed GPU units never exceed capacity."""
    out = []
    for e in events:
        if e["kind"] == "gpu_effect" and e["committed"] > e["capacity"]:
            out.append(V("I1", e["t"], f"committed {e['committed']} > capacity {e['capacity']}"))
    return out


def check_gpu_at_most_once(events):
    """Each op_id started at most once (guaranteed by the GPU; a sanity check)."""
    started, out = set(), []
    for e in events:
        if e["kind"] == "gpu_effect" and e["effect"] == "started":
            if e["op_id"] in started:
                out.append(V("gpu_once", e["t"], f"op {e['op_id']} started twice"))
            started.add(e["op_id"])
    return out


def check_client_liveness(events, accepted=None):
    """I2 (generic form): every client action (or those `accepted(start_record)`
    selects) has an end record by quiesce_at + liveness_window."""
    hdr = scenario_record(events)
    q = hdr.get("quiesce_at")
    w = hdr.get("liveness_window", 30.0)
    deadline = None if q is None else q + w
    starts, ends = _client_ends(events)
    out = []
    for aid in sorted(starts):
        s = starts[aid]
        if accepted is not None and not accepted(s):
            continue
        e = ends.get(aid)
        if e is None:
            out.append(V("I2", None, f"action {aid} ({s['action']}) never resolved"))
        elif deadline is not None and e["t"] > deadline and s["t"] <= deadline:
            out.append(V("I2", e["t"], f"action {aid} ({s['action']}) resolved at "
                                       f"{e['t']:.3f}, after {deadline:.3f}"))
    return out


def check_i3(events, reference_messages):
    """I3: total messages sent <= 3x the reference run's count on the same seed."""
    if reference_messages is None:
        return []
    n = sum(1 for e in events if e["kind"] == "msg_send")
    if n > 3 * int(reference_messages):
        return [V("I3", None, f"{n} messages sent > 3 x reference {reference_messages}")]
    return []


def run_checks(events, codebase=None, reference_messages=None, expect=None):
    """Codebase invariants + I3 (if a reference count is given) + the expect block."""
    out = []
    if codebase is not None:
        out.extend(codebase.check(events))
    out.extend(check_i3(events, reference_messages))
    if expect is None:
        expect = scenario_record(events).get("expect") or {}
    out.extend(evaluate_expect(expect, events,
                               codebase.expect_checks if codebase is not None else None))
    return out
