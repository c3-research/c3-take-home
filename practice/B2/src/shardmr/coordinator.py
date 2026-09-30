"""The shardmr coordinator node.

Client-facing API (`submit_job`, `job_status`, `cancel_job`) and the worker
protocol (`register`, `heartbeat`, `commit`, `report`). The coordinator owns
every job's durable record, the commit log and the reduce stage, and drives
the scheduler and membership loops.

Job lifecycle: PENDING -> MAPPING (attempt n) -> REDUCING -> SUCCEEDED, with
FAILED and CANCELLED as the other terminal states. A job attempt that cannot
make progress (a shard failed `MAX_TASK_FAILURES` times) is abandoned and a new
attempt maps every shard again; commits of an abandoned attempt are never read.
"""

from c3sim import Node, RpcError, handler

from . import config, ids
from .commit import CommitLog
from .gpu_client import GpuClient
from .membership import Membership
from .reduce import ReduceStage
from .scheduler import REVOKED, Scheduler
from .store import (CANCELLED, FAILED, MAPPING, PENDING, REDUCING, SUCCEEDED, TERMINAL,
                    JobStore, new_job, spec_matches, tombstone)


def _parse_spec(p):
    try:
        spec = {"shards": int(p["shards"]), "records": int(p["records"]),
                "units": int(p.get("units", 1)),
                "duration": float(p.get("duration", config.DEFAULT_MAP_DURATION_S))}
    except (KeyError, TypeError, ValueError):
        raise RpcError("INVALID", "shards and records are required integers") from None
    if not 1 <= spec["shards"] <= config.MAX_SHARDS_PER_JOB:
        raise RpcError("INVALID", f"shards must be 1..{config.MAX_SHARDS_PER_JOB}")
    if not 1 <= spec["records"] <= config.MAX_RECORDS_PER_SHARD:
        raise RpcError("INVALID", f"records must be 1..{config.MAX_RECORDS_PER_SHARD}")
    if not 1 <= spec["units"] <= config.MAX_UNITS_PER_SHARD:
        raise RpcError("INVALID", f"units must be 1..{config.MAX_UNITS_PER_SHARD}")
    if spec["duration"] <= 0:
        raise RpcError("INVALID", "duration must be > 0")
    return spec


def _job_ref(p):
    tenant, job_id = p.get("tenant"), p.get("job_id")
    if not isinstance(tenant, str) or not tenant or not isinstance(job_id, str) or not job_id:
        raise RpcError("INVALID", "tenant and job_id must be non-empty strings")
    return tenant, job_id


class Coordinator(Node):
    """Single coordinator of a shardmr deployment."""

    async def on_start(self):
        self.store = JobStore(self.disk)
        self.commits = CommitLog(self.disk)
        self.gpu = GpuClient(self)
        self.gen = self.store.next_generation()
        self.members = Membership(self, self.store)
        self.sched = Scheduler(self)
        self.reducer = ReduceStage(self)
        self.log("coord_start", gen=self.gen, boot=self.boot_count,
                 dead_after=config.worker_dead_after())
        self._recover()
        self.spawn(self._dispatch_loop())
        self.spawn(self._liveness_loop())

    def _recover(self):
        counts = {}
        for jkey in self.store.job_keys():
            rec = self.store.get(jkey)
            state = rec["state"]
            counts[state] = counts.get(state, 0) + 1
            if state == PENDING:
                self.start_attempt(rec, "recovered")
            elif state == MAPPING:
                self.sched.add_job(rec)
            elif state == REDUCING:
                self.reducer.start(jkey)
            elif rec["open_ops"]:
                self.spawn(self.release_open_ops(jkey))
        self.log("coord_recovered", jobs=sum(counts.values()),
                 **{s.lower(): n for s, n in sorted(counts.items())})

    async def _dispatch_loop(self):
        while True:
            await self.sleep(config.DISPATCH_INTERVAL_S)
            self.sched.tick()

    async def _liveness_loop(self):
        while True:
            await self.sleep(config.LIVENESS_CHECK_INTERVAL_S)
            for name in self.members.expire():
                self.sched.revoke_worker(name, "worker_dead")

    # --- job lifecycle ------------------------------------------------------------

    def _set_state(self, rec, state, reason=None):
        old = rec["state"]
        rec["state"] = state
        if reason is not None:
            rec["reason"] = reason
        self.store.put(rec)
        self.log("job_state", job=rec["jkey"], attempt=rec["attempt"], old=old, new=state,
                 reason=reason)

    def start_attempt(self, rec, reason):
        if rec["attempt"] >= config.MAX_JOB_ATTEMPTS:
            self._set_state(rec, FAILED, "attempts_exhausted")
            return
        rec["attempt"] += 1
        rec["reduce_try"] = 0
        self._set_state(rec, MAPPING, reason)
        self.log("attempt_start", job=rec["jkey"], attempt=rec["attempt"],
                 max_attempts=config.MAX_JOB_ATTEMPTS)
        self.sched.add_job(rec)

    def fail_attempt(self, jkey, attempt, reason):
        rec = self.store.get(jkey)
        if rec is None or rec["state"] != MAPPING or rec["attempt"] != attempt:
            return
        self.sched.drop_job(jkey, "attempt_abandoned")
        self.commits.forget(jkey)
        self.log("attempt_abandoned", job=jkey, attempt=attempt, reason=reason)
        self.start_attempt(rec, reason)

    def all_shards_committed(self, jkey, attempt):
        rec = self.store.get(jkey)
        if rec is None or rec["state"] != MAPPING or rec["attempt"] != attempt:
            return
        self._set_state(rec, REDUCING)
        self.sched.drop_job(jkey, "map_complete")
        self.reducer.start(jkey)

    def reopen_mapping(self, jkey, attempt):
        rec = self.store.get(jkey)
        if rec is None or rec["state"] != REDUCING or rec["attempt"] != attempt:
            return
        self._set_state(rec, MAPPING, "reduce_incomplete")
        self.sched.add_job(rec)

    def complete_job(self, jkey, attempt, op_id, result):
        rec = self.store.get(jkey)
        if rec is None or rec["state"] != REDUCING or rec["attempt"] != attempt:
            self.log("reduce_discarded", job=jkey, attempt=attempt, op_id=op_id,
                     state=None if rec is None else rec["state"])
            return False
        rec["result"] = result
        self._set_state(rec, SUCCEEDED)
        self.log("job_succeeded", job=jkey, attempt=attempt, op_id=op_id,
                 count=result["count"], checksum=result["checksum"])
        return True

    def reduce_failed(self, jkey, attempt, op_id):
        rec = self.store.get(jkey)
        if rec is None or rec["state"] != REDUCING or rec["attempt"] != attempt:
            return
        rec["reduce_try"] += 1
        self.log("reduce_failed", job=jkey, attempt=attempt, op_id=op_id,
                 tries=rec["reduce_try"], max_tries=config.MAX_REDUCE_FAILURES)
        if rec["reduce_try"] >= config.MAX_REDUCE_FAILURES:
            self._set_state(rec, FAILED, "reduce_failures")
        else:
            self.store.put(rec)

    async def release_open_ops(self, jkey):
        """Release every GPU operation a terminal job still has open."""
        rec = self.store.get(jkey)
        if rec is None or rec["state"] not in TERMINAL:
            return
        for op_id in list(rec["open_ops"]):
            st = await self.gpu.finish(op_id)
            self.log("open_op_released", job=jkey, op_id=op_id,
                     state=None if st is None else st["state"])
            self.store.drop_open_op(jkey, op_id)

    # --- client API -------------------------------------------------------------------

    @handler("submit_job")
    async def submit_job(self, src, p):
        tenant, job_id = _job_ref(p)
        spec = _parse_spec(p)
        jkey = ids.job_key(tenant, job_id)
        rec = self.store.get(jkey)
        if rec is not None:
            if not spec_matches(rec, spec):
                self.log("job_conflict", job=jkey)
                raise RpcError("CONFLICT", f"job {job_id} exists with different parameters")
            if rec["spec"] is None:
                rec["spec"] = spec
                self.store.put(rec)
            self.log("job_duplicate", job=jkey, state=rec["state"])
            return {"accepted": True, "state": rec["state"], "attempt": rec["attempt"]}
        rec = new_job(tenant, job_id, spec)
        self.store.put(rec)
        self.log("job_accepted", job=jkey, shards=spec["shards"], records=spec["records"],
                 units=spec["units"])
        self.start_attempt(rec, "submitted")
        return {"accepted": True, "state": rec["state"], "attempt": rec["attempt"]}

    @handler("job_status")
    async def job_status(self, src, p):
        tenant, job_id = _job_ref(p)
        rec = self.store.get(ids.job_key(tenant, job_id))
        if rec is None:
            raise RpcError("NOT_FOUND", f"no job {job_id}")
        out = {"state": rec["state"], "attempt": rec["attempt"]}
        if rec["spec"] is not None:
            out["shards"] = rec["spec"]["shards"]
            if rec["attempt"] > 0:
                out["committed"] = self.commits.count(rec["jkey"], rec["attempt"])
        if rec["state"] == SUCCEEDED:
            out["result"] = rec["result"]
        return out

    @handler("cancel_job")
    async def cancel_job(self, src, p):
        tenant, job_id = _job_ref(p)
        jkey = ids.job_key(tenant, job_id)
        rec = self.store.get(jkey)
        if rec is None:
            self.store.put(tombstone(tenant, job_id))
            self.log("job_cancel_recorded", job=jkey)
            return {"state": CANCELLED}
        if rec["state"] in TERMINAL:
            return {"state": rec["state"]}
        was = rec["state"]
        self._set_state(rec, CANCELLED, "client_cancel")
        self.sched.drop_job(jkey, "job_cancelled")
        if was != REDUCING and rec["open_ops"]:
            self.spawn(self.release_open_ops(jkey))
        return {"state": CANCELLED}

    # --- worker protocol ---------------------------------------------------------------

    @handler("register")
    async def register(self, src, p):
        worker = str(p["worker"])
        epoch, fresh = self.members.register(worker, p["boot"], p["reg_seq"])
        if fresh:
            self.sched.revoke_worker(worker, "reregistered")
        return {"epoch": epoch, "gen": self.gen}

    @handler("heartbeat")
    async def heartbeat(self, src, p):
        worker = str(p["worker"])
        info, fresh = self.members.heartbeat(worker, p["epoch"], p["hb_seq"])
        if info is None:
            return {"reregister": True}
        abort = self.sched.on_heartbeat(info, p.get("running", []), int(p["hb_seq"])) if fresh else []
        return {"ok": True, "abort": abort}

    @handler("commit")
    async def commit(self, src, p):
        worker, tid = str(p["worker"]), str(p["task"])
        jkey, attempt, shard = str(p["job"]), int(p["attempt"]), int(p["shard"])
        if not self.members.is_current(worker, p["epoch"]):
            self.log("commit_rejected", task=tid, worker=worker, reason="fenced",
                     epoch=p["epoch"])
            raise RpcError("FENCED", f"{worker} epoch {p['epoch']} is not current")
        rec = self.store.get(jkey)
        if rec is None:
            raise RpcError("NOT_FOUND", f"no job {jkey}")
        if attempt != rec["attempt"]:
            self.log("commit_rejected", task=tid, worker=worker, reason="stale_attempt",
                     attempt=attempt, current=rec["attempt"])
            raise RpcError("STALE_ATTEMPT", f"attempt {attempt} != {rec['attempt']}")
        output = p.get("output") or {}
        if output.get("shard") != shard:
            raise RpcError("INVALID", "output does not belong to this shard")
        if rec["state"] == MAPPING:
            t = self.sched.tasks.get(tid)
            if (t is None or t.worker != worker or t.jkey != jkey or t.attempt != attempt
                    or t.shard != shard):
                self.log("commit_rejected", task=tid, worker=worker, reason="unknown_task")
                raise RpcError("UNKNOWN_TASK", tid)
            if t.state == REVOKED:
                self.log("commit_rejected", task=tid, worker=worker, reason="revoked")
                raise RpcError("REVOKED", tid)
            won, crec, fresh = self.commits.try_commit(jkey, attempt, shard, tid, worker,
                                                       output)
            if fresh:
                self.log("shard_committed", job=jkey, attempt=attempt, shard=shard, task=tid,
                         worker=worker, speculative=t.speculative,
                         committed=self.commits.count(jkey, attempt),
                         shards=rec["spec"]["shards"])
                self.sched.on_commit(jkey, attempt, shard, tid)
        elif rec["state"] == CANCELLED:
            return {"committed": False, "winner": None, "reason": "job_closed"}
        else:
            crec = self.commits.get(jkey, attempt, shard)
            won = crec is not None and crec["task"] == tid
        if not won:
            self.log("commit_lost", task=tid, worker=worker,
                     winner=None if crec is None else crec["task"])
        return {"committed": won, "winner": None if crec is None else crec["task"]}

    @handler("report")
    async def report(self, src, p):
        worker = str(p["worker"])
        if not self.members.is_current(worker, p["epoch"]):
            raise RpcError("FENCED", f"{worker} epoch {p['epoch']} is not current")
        self.sched.on_report(worker, int(p["epoch"]), str(p["batch"]), p.get("results", []))
        return {"ok": True}

    @handler("release_complete")
    async def release_complete(self, src, p):
        self.gpu.on_release_complete(str(p.get("op_id")))

