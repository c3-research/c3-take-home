"""Map-task scheduling: queueing, batching, reconciliation and speculation.

The scheduler owns the coordinator's in-memory view of every map task of the
current attempt of each running job. Shards that need a task sit in a FIFO
queue; every dispatch tick hands each live worker a batch of up to
`MAX_BATCH_TASKS` queued shards, bounded by its free slots. Workers report a
batch once all its tasks have finished, with one outcome per task, so a batch
can partially fail: only the failed shards are retried.

A running task that exceeds its shard's duration plus `STRAGGLER_SLACK_S` gets
one speculative duplicate on another worker. Duplicates race to commit; the
commit log keeps the first. The losers are told to abort in their worker's next
heartbeat reply; one that finishes before then offers its output, which the
commit log turns away.
"""

import math

from c3sim import RpcError, RpcTimeout

from . import config, ids

ASSIGNING = "assigning"
RUNNING = "running"
DONE = "done"
LOST = "lost"
REVOKED = "revoked"
LIVE = (ASSIGNING, RUNNING)


class TaskRec:
    __slots__ = ("tid", "jkey", "attempt", "shard", "worker", "epoch", "speculative",
                 "state", "dispatched_at", "ack_seq", "batch", "outcome")

    def __init__(self, tid, jkey, attempt, shard, worker, epoch, speculative, now, batch):
        self.tid = tid
        self.jkey = jkey
        self.attempt = attempt
        self.shard = shard
        self.worker = worker
        self.epoch = epoch
        self.speculative = speculative
        self.state = ASSIGNING
        self.dispatched_at = now
        self.ack_seq = None
        self.batch = batch
        self.outcome = None


class ShardState:
    __slots__ = ("failures", "live", "speculated", "queued")

    def __init__(self):
        self.failures = 0
        self.live = set()
        self.speculated = 0
        self.queued = False


class JobProgress:
    """Scheduling state of one attempt of one job."""

    def __init__(self, rec):
        self.jkey = rec["jkey"]
        self.tenant = rec["tenant"]
        self.attempt = int(rec["attempt"])
        self.spec = dict(rec["spec"])
        self.shards = {i: ShardState() for i in range(int(self.spec["shards"]))}


class Scheduler:
    def __init__(self, coord):
        self.c = coord
        self.tasks = {}
        self.jobs = {}
        self.queue = []
        self.by_worker = {}
        self.superseded = {}
        self._task_n = 0
        self._batch_n = 0

    # --- bookkeeping ------------------------------------------------------------

    def load(self, worker):
        return len(self.by_worker.get(worker, ()))

    def _free_slots(self, worker):
        return config.WORKER_SLOTS - self.load(worker)

    def _committed(self, jp, shard):
        return self.c.commits.get(jp.jkey, jp.attempt, shard) is not None

    def add_job(self, rec):
        """Start scheduling the current attempt of `rec`."""
        jp = JobProgress(rec)
        self.jobs[jp.jkey] = jp
        pending = [s for s in sorted(jp.shards) if not self._committed(jp, s)]
        for s in pending:
            self._enqueue(jp, s)
        self.c.log("job_scheduled", job=jp.jkey, attempt=jp.attempt, shards=len(jp.shards),
                   queued=len(pending))
        if not pending:
            self.c.all_shards_committed(jp.jkey, jp.attempt)

    def drop_job(self, jkey, reason):
        """Stop scheduling a job: revoke its live tasks and clear its queue entries."""
        jp = self.jobs.pop(jkey, None)
        if jp is None:
            return
        self.queue = [q for q in self.queue if q[0] != jkey]
        aborts = {}
        for s in sorted(jp.shards):
            for tid in sorted(jp.shards[s].live):
                t = self.tasks[tid]
                self._finish(t, REVOKED, reason)
                aborts.setdefault(t.worker, []).append(tid)
        for w in sorted(aborts):
            self._send_abort(w, aborts[w])
        for tid in [t for t, rec in self.tasks.items() if rec.jkey == jkey]:
            del self.tasks[tid]

    def _enqueue(self, jp, shard):
        ss = jp.shards[shard]
        if ss.queued:
            return
        ss.queued = True
        self.queue.append((jp.jkey, jp.attempt, shard))

    def _finish(self, t, state, outcome):
        if t.state not in LIVE:
            return False
        t.state = state
        t.outcome = outcome
        jp = self.jobs.get(t.jkey)
        if jp is not None and jp.attempt == t.attempt:
            jp.shards[t.shard].live.discard(t.tid)
        live = self.by_worker.get(t.worker)
        if live is not None:
            live.discard(t.tid)
        self.c.log("task_finished", task=t.tid, worker=t.worker, state=state, outcome=outcome)
        return True

    def _settle(self, jkey, shard):
        """Make sure an uncommitted shard of a running job has a task or a queue slot."""
        jp = self.jobs.get(jkey)
        if jp is None:
            return
        ss = jp.shards[shard]
        if self._committed(jp, shard) or ss.live or ss.queued:
            return
        if ss.failures >= config.MAX_TASK_FAILURES:
            self.c.log("shard_exhausted", job=jkey, attempt=jp.attempt, shard=shard,
                       failures=ss.failures, max_failures=config.MAX_TASK_FAILURES)
            self.c.fail_attempt(jkey, jp.attempt, "shard_failures")
            return
        self._enqueue(jp, shard)
        self.c.log("shard_requeued", job=jkey, attempt=jp.attempt, shard=shard,
                   failures=ss.failures)

    # --- dispatch -------------------------------------------------------------------

    def _new_task(self, jp, shard, info, speculative, batch):
        self._task_n += 1
        tid = ids.task_id(jp.jkey, jp.attempt, shard, self.c.gen, self._task_n)
        t = TaskRec(tid, jp.jkey, jp.attempt, shard, info.name, info.epoch, speculative,
                    self.c.clock.now(), batch)
        self.tasks[tid] = t
        jp.shards[shard].live.add(tid)
        self.by_worker.setdefault(info.name, set()).add(tid)
        self.c.log("task_dispatch", task=tid, job=jp.jkey, attempt=jp.attempt, shard=shard,
                   worker=info.name, epoch=info.epoch, speculative=speculative, batch=batch)
        return t

    def _descriptor(self, t):
        jp = self.jobs[t.jkey]
        return {"task": t.tid, "job": t.jkey, "tenant": jp.tenant, "attempt": t.attempt,
                "shard": t.shard, "records": int(jp.spec["records"]),
                "units": int(jp.spec["units"]), "duration": float(jp.spec["duration"]),
                "speculative": t.speculative}

    def _pop_queued(self):
        while self.queue:
            jkey, attempt, shard = self.queue.pop(0)
            jp = self.jobs.get(jkey)
            if jp is None or jp.attempt != attempt:
                continue
            ss = jp.shards[shard]
            if not ss.queued:
                continue
            ss.queued = False
            if self._committed(jp, shard):
                continue
            return jp, shard
        return None

    def tick(self):
        """One dispatch round: fill free worker slots, then consider speculation."""
        if not self.jobs:
            return
        workers = sorted(self.c.members.dispatchable(), key=lambda i: (self.load(i.name), i.name))
        for info in workers:
            n = min(self._free_slots(info.name), config.MAX_BATCH_TASKS)
            picked = []
            while n > 0:
                item = self._pop_queued()
                if item is None:
                    break
                picked.append(item)
                n -= 1
            if picked:
                self._dispatch_batch(info, picked, speculative=False)
            if not self.queue:
                break
        self._speculate()

    def _dispatch_batch(self, info, items, speculative):
        self._batch_n += 1
        bid = ids.batch_id(info.name, info.epoch, self._batch_n)
        tasks = [self._new_task(jp, shard, info, speculative, bid) for jp, shard in items]
        self.c.spawn(self._send_batch(info.name, info.epoch, bid, [t.tid for t in tasks]))

    async def _send_batch(self, worker, epoch, bid, tids):
        tids = [t for t in tids if t in self.tasks and self.tasks[t].state == ASSIGNING
                and self.tasks[t].jkey in self.jobs]
        if not tids:
            self.c.log("batch_assign_skipped", batch=bid, worker=worker)
            return
        payload = {"worker": worker, "epoch": epoch, "batch": bid,
                   "tasks": [self._descriptor(self.tasks[t]) for t in tids]}
        for attempt in range(1, config.ASSIGN_MAX_ATTEMPTS + 1):
            try:
                r = await self.c.rpc(worker, "assign", payload, config.ASSIGN_RPC_TIMEOUT_S)
            except RpcTimeout:
                self.c.log("batch_assign_retry", batch=bid, worker=worker, attempt=attempt)
                continue
            except RpcError as e:
                self.c.log("batch_assign_rejected", batch=bid, worker=worker, code=e.code)
                self._abandon(tids, REVOKED, "assign_rejected")
                return
            accepted = set(r.get("accepted", []))
            info = self.c.members.workers.get(worker)
            seen = info.last_hb_seq if info is not None else 0
            for tid in tids:
                t = self.tasks.get(tid)
                if t is None or t.state != ASSIGNING:
                    continue
                if tid in accepted:
                    t.state = RUNNING
                    t.ack_seq = min(seen, int(r.get("hb_seq", 0)))
                else:
                    self._abandon([tid], LOST, "not_accepted")
            self.c.log("batch_assigned", batch=bid, worker=worker, tasks=len(tids),
                       accepted=len(accepted), hb_seq=r.get("hb_seq"))
            return
        self.c.log("batch_assign_failed", batch=bid, worker=worker,
                   attempts=config.ASSIGN_MAX_ATTEMPTS)
        self._send_abort(worker, list(tids))
        self._abandon(tids, LOST, "assign_timeout")

    def _abandon(self, tids, state, outcome):
        for tid in tids:
            t = self.tasks.get(tid)
            if t is not None and self._finish(t, state, outcome):
                self._settle(t.jkey, t.shard)

    def _send_abort(self, worker, tids):
        if tids:
            self.c.send(worker, "abort", {"tasks": sorted(tids)})

    # --- speculation ------------------------------------------------------------------

    def _speculate(self):
        now = self.c.clock.now()
        for jkey in sorted(self.jobs):
            jp = self.jobs[jkey]
            n = len(jp.shards)
            done = self.c.commits.count(jkey, jp.attempt)
            if done < math.ceil(config.SPECULATION_MIN_COMMITTED_FRACTION * n):
                continue
            straggler_after = float(jp.spec["duration"]) + config.STRAGGLER_SLACK_S
            for shard in sorted(jp.shards):
                ss = jp.shards[shard]
                if (ss.queued or len(ss.live) != 1
                        or ss.speculated >= config.MAX_SPECULATIVE_PER_SHARD
                        or self._committed(jp, shard)):
                    continue
                t = self.tasks[next(iter(ss.live))]
                if t.state != RUNNING or now - t.dispatched_at <= straggler_after:
                    continue
                target = self._pick_other(t.worker)
                if target is None:
                    return
                ss.speculated += 1
                self.c.log("speculation_launch", job=jkey, attempt=jp.attempt, shard=shard,
                           straggler=t.tid, worker=target.name,
                           elapsed=round(now - t.dispatched_at, 6),
                           straggler_after=straggler_after)
                self._dispatch_batch(target, [(jp, shard)], speculative=True)

    def _pick_other(self, worker):
        best = None
        for info in self.c.members.dispatchable():
            if info.name == worker or self._free_slots(info.name) <= 0:
                continue
            if best is None or self.load(info.name) < self.load(best.name):
                best = info
        return best

    # --- events from workers and the commit log ---------------------------------------------

    def on_commit(self, jkey, attempt, shard, winner):
        """A shard committed: abort its other live tasks."""
        jp = self.jobs.get(jkey)
        if jp is None or jp.attempt != attempt:
            return
        losers = {}
        for tid in sorted(jp.shards[shard].live):
            if tid != winner:
                losers.setdefault(self.tasks[tid].worker, []).append(tid)
        for w in sorted(losers):
            self.superseded.setdefault(w, set()).update(losers[w])
            self.c.log("task_abort_request", job=jkey, shard=shard, worker=w,
                       tasks=len(losers[w]), reason="shard_committed")
        if self.c.commits.count(jkey, attempt) == len(jp.shards):
            self.c.all_shards_committed(jkey, attempt)

    def on_report(self, worker, epoch, bid, results):
        """Apply a batch report. Each task's outcome is handled on its own."""
        failed = 0
        for r in results:
            t = self.tasks.get(r.get("task"))
            if t is None or t.worker != worker or t.epoch != epoch:
                continue
            outcome = str(r.get("outcome"))
            if not self._finish(t, DONE, outcome):
                continue
            jp = self.jobs.get(t.jkey)
            if jp is None or jp.attempt != t.attempt:
                continue
            if outcome == "failed":
                failed += 1
                jp.shards[t.shard].failures += 1
            self._settle(t.jkey, t.shard)
        self.c.log("batch_report", batch=bid, worker=worker, tasks=len(results), failed=failed)

    def on_heartbeat(self, info, running, hb_seq):
        """Reconcile a worker's running list. Returns task ids the worker must abort."""
        running = set(running)
        abort = []
        superseded = self.superseded.pop(info.name, set())
        for tid in sorted(running):
            t = self.tasks.get(tid)
            if (t is None or t.state not in LIVE or t.worker != info.name
                    or tid in superseded):
                abort.append(tid)
        for tid in sorted(self.by_worker.get(info.name, ())):
            t = self.tasks[tid]
            if (t.state == RUNNING and t.ack_seq is not None and hb_seq > t.ack_seq
                    and tid not in running):
                self._abandon([tid], LOST, "missing_from_heartbeat")
        return abort

    def revoke_worker(self, worker, reason):
        """Revoke every live task held by `worker` (it was fenced or re-registered)."""
        tids = sorted(self.by_worker.get(worker, ()))
        if tids:
            self.c.log("worker_tasks_revoked", worker=worker, tasks=len(tids), reason=reason)
        self._abandon(tids, REVOKED, reason)
