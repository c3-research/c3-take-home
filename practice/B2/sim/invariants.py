"""shardmr invariants (see INVARIANTS.md). check(events) -> [Violation].

Only the trace is read. The reference result of a job is recomputed here from
the job's submitted parameters, independently of the service code.
"""

import hashlib

from c3sim.check import (check_client_liveness, check_gpu_at_most_once, check_i0,
                         check_i1)


def V(inv, t, detail):
    return {"invariant": inv, "t": t, "detail": detail}


# --- reference computation (independent of src/) -----------------------------------

def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()


def reference_output(jkey, shard, records):
    recs = [int(_h(f"{jkey}#{int(shard)}#{k}")[:8], 16) % 10007 for k in range(int(records))]
    return {"shard": int(shard), "count": len(recs), "sum": sum(recs),
            "digest": _h(",".join(str(r) for r in recs))[:16]}


def reference_result(jkey, shards, records):
    outs = [reference_output(jkey, s, records) for s in range(int(shards))]
    return {"shards": len(outs), "count": sum(o["count"] for o in outs),
            "sum": sum(o["sum"] for o in outs),
            "checksum": _h("|".join(f"{o['shard']}:{o['digest']}" for o in outs))[:16]}


# --- trace indexing ------------------------------------------------------------------

def _jobs(events):
    """(tenant:job_id) -> submitted parameters, from client start records."""
    jobs = {}
    for e in events:
        if e["kind"] == "client" and e.get("phase") == "start" and e.get("action") == "submit_job":
            jobs.setdefault(f"{e['tenant']}:{e['job_id']}",
                            {"shards": int(e["shards"]), "records": int(e["records"])})
    return jobs


def _commits(events):
    """Every commit the coordinator granted, from its replies and its log.

    Returns {(job, attempt, shard): [(t, task, output_or_None), ...]}.
    """
    reqs, out = {}, {}
    for e in events:
        k = e["kind"]
        if k == "msg_send" and e.get("method") == "commit":
            if e.get("type") == "req":
                reqs[e["msg_id"]] = e.get("payload") or {}
            elif e.get("type") == "resp":
                body = (e.get("payload") or {}).get("ok")
                req = reqs.get(e.get("rpc_id"))
                if body and body.get("committed") and req is not None:
                    key = (req["job"], int(req["attempt"]), int(req["shard"]))
                    out.setdefault(key, []).append((e["t"], req["task"], req.get("output")))
        elif k == "log" and e.get("event") == "shard_committed":
            key = (e["job"], int(e["attempt"]), int(e["shard"]))
            out.setdefault(key, []).append((e["t"], e["task"], None))
    return out


def _accepted_ops(events, kind):
    for e in events:
        if (e["kind"] == "gpu_effect" and e.get("effect") == "accepted"
                and (e.get("payload") or {}).get("kind") == kind):
            yield e


# --- B1 ------------------------------------------------------------------------------

def check_b1(events):
    """B1: each shard of a job attempt is committed by at most one task, and each
    map task runs as at most one GPU operation."""
    out = []
    for key, grants in sorted(_commits(events).items()):
        tasks = sorted({task for _, task, _ in grants})
        if len(tasks) > 1:
            t = min(g[0] for g in grants if g[1] == tasks[1])
            out.append(V("B1", t, f"job {key[0]} attempt {key[1]} shard {key[2]} committed by "
                                  f"{len(tasks)} tasks: {', '.join(tasks)}"))
    ops = {}
    for e in _accepted_ops(events, "map"):
        ops.setdefault(e["payload"]["task"], []).append(e)
    for task, recs in sorted(ops.items()):
        ids = sorted({r["op_id"] for r in recs})
        if len(ids) > 1:
            out.append(V("B1", recs[1]["t"], f"map task {task} ran as {len(ids)} GPU operations: "
                                             f"{', '.join(ids)}"))
    return out


# --- B2 ------------------------------------------------------------------------------

def check_b2(events):
    """B2: a reduce reads exactly the committed output of every shard, once each,
    all from the attempt it reduces."""
    out = []
    jobs = _jobs(events)
    grants = _commits(events)
    for e in _accepted_ops(events, "reduce"):
        p = e["payload"]
        job, attempt = p["job"], int(p["attempt"])
        want = jobs.get(job, {}).get("shards", p.get("shards"))
        where = f"reduce {e['op_id']}"
        seen = {}
        for inp in p.get("inputs", []):
            s = int(inp["shard"])
            seen[s] = seen.get(s, 0) + 1
            if int(inp.get("attempt", -1)) != attempt:
                out.append(V("B2", e["t"], f"{where} reads shard {s} from attempt "
                                           f"{inp.get('attempt')} while reducing attempt {attempt}"))
                continue
            g = [x for x in grants.get((job, attempt, s), []) if x[0] <= e["t"]]
            if not g:
                out.append(V("B2", e["t"], f"{where} reads shard {s} with no commit "
                                           f"in attempt {attempt}"))
                continue
            winner = min(g, key=lambda x: x[0])[1]
            if inp.get("task") != winner:
                out.append(V("B2", e["t"], f"{where} reads shard {s} from task {inp.get('task')}, "
                                           f"committed task is {winner}"))
                continue
            outputs = [x[2] for x in g if x[1] == winner and x[2] is not None]
            if outputs and inp.get("output") != outputs[0]:
                out.append(V("B2", e["t"], f"{where} reads shard {s} with an output that differs "
                                           f"from the committed one"))
        dup = sorted(s for s, n in seen.items() if n > 1)
        if dup:
            out.append(V("B2", e["t"], f"{where} reads shards {dup} more than once"))
        if want is not None:
            missing = sorted(set(range(int(want))) - set(seen))
            if missing:
                out.append(V("B2", e["t"], f"{where} is missing shards {missing[:10]}"))
    return out


# --- B3 ------------------------------------------------------------------------------

def check_b3(events):
    """B3: every job that completes successfully returns the reference result."""
    out = []
    for e in events:
        if not (e["kind"] == "client" and e.get("phase") == "end"
                and e.get("action") == "submit_job" and e.get("outcome") == "ok"):
            continue
        jkey = f"{e['tenant']}:{e['job_id']}"
        want = reference_result(jkey, e["shards"], e["records"])
        got = (e.get("result") or {}).get("result")
        if got != want:
            out.append(V("B3", e["t"], f"job {jkey} returned {got}, reference {want}"))
    return out


def check(events):
    out = []
    out += check_i0(events)
    out += check_i1(events)
    out += check_gpu_at_most_once(events)
    out += check_client_liveness(events)
    out += check_b1(events)
    out += check_b2(events)
    out += check_b3(events)
    return out
