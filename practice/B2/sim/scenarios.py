"""Scenario loader for shardmr (copied to case/sim/scenarios.py in a case).

Workload actions (see scenario_schema.md):

    submit_job   submit a job, then poll job_status until it is terminal
    cancel_job   ask the coordinator to cancel a job
    job_status   one status query (generic action)
"""

from c3sim import RpcError, RpcTimeout

DEFAULT_TARGET = "coord"

POLL_INTERVAL_S = 1.0
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
JOB_FIELDS = ("tenant", "job_id", "shards", "records", "units", "duration")


async def submit_job(client, args):
    payload = {k: args[k] for k in JOB_FIELDS if k in args}
    r = await client.call("submit_job", payload)
    client.event("accepted", job_id=args["job_id"], tenant=args["tenant"], state=r.get("state"))
    ref = {"tenant": args["tenant"], "job_id": args["job_id"]}
    poll = float(args.get("poll", POLL_INTERVAL_S))
    while True:
        await client.sleep(poll)
        try:
            st = await client.rpc(client.target, "job_status", ref, client.retry["timeout"])
        except (RpcTimeout, RpcError):
            continue
        if st["state"] in TERMINAL:
            outcome = {"SUCCEEDED": "ok", "FAILED": "failed", "CANCELLED": "cancelled"}[st["state"]]
            return {"outcome": outcome, "result": st}


async def cancel_job(client, args):
    r = await client.call("cancel_job", {"tenant": args["tenant"], "job_id": args["job_id"]})
    return {"outcome": "ok", "result": r}


ACTIONS = {"submit_job": submit_job, "cancel_job": cancel_job}


def _max_outcome(name):
    def check(value, events):
        n = sum(1 for e in events if e["kind"] == "client" and e.get("phase") == "end"
                and e.get("action") == "submit_job" and e.get("outcome") == name)
        if n > int(value):
            return [{"invariant": "expect", "t": None,
                     "detail": f"{n} submit_job actions ended {name} (max {value})"}]
        return []
    return check


EXPECT = {"max_failed_jobs": _max_outcome("failed"), "max_timed_out_jobs": _max_outcome("timeout")}
