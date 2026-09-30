"""Scenario loader for the job router (copied to case/sim/scenarios.py in a case).

Workload actions (see scenario_schema.md):

    submit_job {tenant, job_id, units, duration}   submit, then poll until terminal
    cancel_job {tenant, job_id}                    cancel, then poll until terminal
    job_status {tenant, job_id}                    one status call

The client uses the scenario's client_retry policy for the submit and cancel
calls, and polls job status every POLL_S seconds until the job is terminal.
"""

from c3sim import RpcError, RpcTimeout

DEFAULT_TARGET = "router-1"
POLL_S = 1.0
POLL_TIMEOUT_S = 1.0
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
OUTCOME = {"SUCCEEDED": "ok", "FAILED": "failed", "CANCELLED": "cancelled"}


def _ref(args):
    return {"tenant": str(args.get("tenant", "default")), "job_id": str(args["job_id"])}


async def _wait_terminal(client, ref):
    while True:
        await client.sleep(POLL_S)
        try:
            st = await client.rpc(client.target, "status", ref, POLL_TIMEOUT_S)
        except (RpcTimeout, RpcError):
            continue
        if st.get("state") in TERMINAL:
            return st


def _result(st):
    out = {"state": st["state"], "attempt": st.get("attempt")}
    if st.get("result") is not None:
        out["digest"] = st["result"].get("digest")
        out["op_id"] = st.get("op_id")
    return out


async def submit_job(client, args):
    ref = _ref(args)
    req = dict(ref, units=int(args.get("units", 1)), duration=float(args.get("duration", 1.0)))
    r = await client.call("submit", req)
    client.event("accepted", tenant=ref["tenant"], job_id=ref["job_id"], state=r.get("state"))
    st = await _wait_terminal(client, ref)
    return {"outcome": OUTCOME[st["state"]], "result": _result(st)}


async def cancel_job(client, args):
    ref = _ref(args)
    r = await client.call("cancel", ref)
    client.event("cancel_acknowledged", tenant=ref["tenant"], job_id=ref["job_id"],
                 state=r.get("state"))
    if r.get("state") in TERMINAL:
        return {"outcome": "ok", "result": {"state": r["state"]}}
    st = await _wait_terminal(client, ref)
    return {"outcome": "ok", "result": _result(st)}


async def job_status(client, args):
    ref = _ref(args)
    st = await client.call("status", ref)
    return {"outcome": "ok", "result": _result(st) if st.get("state") in TERMINAL
            else {"state": st.get("state")}}


ACTIONS = {"submit_job": submit_job, "cancel_job": cancel_job, "job_status": job_status}


def _log_count(event):
    def check(value, events):
        n = sum(1 for e in events if e["kind"] == "log" and e.get("event") == event)
        if n > int(value):
            return [{"invariant": "expect", "t": None,
                     "detail": f"{n} {event} records (max {value})"}]
        return []
    return check


def _min_succeeded(value, events):
    n = sum(1 for e in events if e["kind"] == "log" and e.get("event") == "job_terminal"
            and e.get("state") == "SUCCEEDED")
    if n < int(value):
        return [{"invariant": "expect", "t": None,
                 "detail": f"{n} jobs succeeded (min {value})"}]
    return []


EXPECT = {
    "max_capacity_waits": _log_count("gpu_capacity_wait"),
    "max_fenced": _log_count("lease_fenced"),
    "min_jobs_succeeded": _min_succeeded,
}


def transform(scenario):
    """State the GPU capacity the service is deployed against (service.gpu_capacity)."""
    gpu = scenario.get("gpu") or {}
    service = dict(scenario.get("service") or {})
    if "capacity" in gpu and "gpu_capacity" not in service:
        service["gpu_capacity"] = int(gpu["capacity"])
    scenario["service"] = service
    return scenario
