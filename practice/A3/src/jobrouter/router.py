"""The router: owns job state, grants leases and accounts GPU capacity.

Client API (tenant-facing, idempotent by ``(tenant, job_id)``):

    submit {tenant, job_id, units, duration} -> {accepted, state}
    status {tenant, job_id}                  -> job view (see models.public_view)
    cancel {tenant, job_id}                  -> {state}

Holder API (workers):

    hello   {boot}                        -> {ok}         register an incarnation
    acquire {req_id, boot, slot}          -> {lease|None} lease the next job that fits
    renew   {key, epoch, boot}            -> {ok, cancel} extend a lease
    report  {key, epoch, boot, op_id, state, result?} -> {ok, state}

Admin API:

    stats   {}                            -> counters and gauges (see metrics.py)

Every change is written to the job's disk record before the reply is sent.
Handlers do not await between reading and writing a record, so each handler's
state change is atomic with respect to other handlers on this node.
"""

from c3sim import Node, RpcError, handler

from . import config, models
from .capacity import CapacityLedger
from .dispatch import Dispatcher
from .leases import LeaseTable
from .metrics import RouterMetrics
from .recovery import recover
from .releaser import ReleaseManager
from .store import JobStore


class Router(Node):
    async def on_start(self):
        self.store = JobStore(self)
        self.leases = LeaseTable(self.clock, config.LEASE_S)
        self.capacity = CapacityLedger(self, config.gpu_capacity(self.config))
        self.releaser = ReleaseManager(self, self._released)
        self.dispatcher = Dispatcher(self, self.store, self.capacity)
        self.metrics = RouterMetrics(self)
        self.worker_boots = {}
        self.acquire_cache = {}
        summary = recover(self)
        self.log("router_start", boot=self.boot_count, capacity=self.capacity.capacity,
                 lease_s=config.LEASE_S, regrant_after=round(config.regrant_after(), 6),
                 **summary)
        self.spawn(self._lease_monitor())
        self.spawn(self.metrics.loop())

    # =====================================================================================
    # Client API
    # =====================================================================================

    @handler("submit")
    async def submit(self, src, p):
        tenant, job_id, units, duration = models.parse_submit(p)
        if units > self.capacity.capacity:
            raise RpcError("INVALID", f"units {units} exceed deployment capacity "
                                      f"{self.capacity.capacity}")
        job = self.store.lookup(tenant, job_id)
        if job is not None and job["precancelled"] and job["units"] is None:
            job["units"] = units
            job["duration"] = duration
            self.store.put(job)
            self.log("job_accepted", key=job["key"], tenant=tenant, job_id=job_id, units=units,
                     duration=duration, state=job["state"])
            return {"accepted": True, "state": job["state"]}
        if job is not None:
            if not models.same_submission(job, units, duration):
                self.log("submit_mismatch", key=job["key"])
                raise RpcError("MISMATCH", "job_id already used with different parameters")
            self.metrics.incr("duplicates")
            self.log("submit_duplicate", key=job["key"], state=job["state"])
            return {"accepted": True, "state": job["state"]}
        job = models.new_job(tenant, job_id, units, duration, self.store.next_seq())
        self.store.put(job)
        self.metrics.incr("submitted")
        self.log("job_accepted", key=job["key"], tenant=tenant, job_id=job_id, units=units,
                 duration=duration, state=job["state"])
        return {"accepted": True, "state": job["state"]}

    @handler("status")
    async def status(self, src, p):
        tenant, job_id = models.parse_job_ref(p)
        job = self.store.lookup(tenant, job_id)
        if job is None or (job["precancelled"] and job["units"] is None):
            raise RpcError("NOT_FOUND", "no such job")
        return models.public_view(job)

    @handler("cancel")
    async def cancel(self, src, p):
        tenant, job_id = models.parse_job_ref(p)
        job = self.store.lookup(tenant, job_id)
        self.metrics.incr("cancels")
        if job is None:
            job = models.tombstone(tenant, job_id, self.store.next_seq())
            self.store.put(job)
            self._log_terminal(job, reason="precancel")
            return {"state": job["state"]}
        if models.is_terminal(job):
            self.log("cancel_noop", key=job["key"], state=job["state"])
            return {"state": job["state"]}
        if job["cancel"]:
            return {"state": "CANCELLING"}
        if job["state"] == models.QUEUED and not job["granted"]:
            job["state"] = models.CANCELLED
            job["cancel"] = True
            self.store.put(job)
            self.dispatcher.forget(job["key"])
            self._log_terminal(job, reason="cancel_queued")
            return {"state": job["state"]}
        job["cancel"] = True
        self.store.put(job)
        self.log("cancel_requested", key=job["key"], state=job["state"], epoch=job["epoch"],
                 op_id=models.current_op_id(job))
        return {"state": "CANCELLING"}

    # =====================================================================================
    # Holder API
    # =====================================================================================

    @handler("hello")
    async def hello(self, src, p):
        boot = int(p.get("boot", 0))
        self._note_boot(src, boot)
        return {"ok": True}

    def _note_boot(self, worker, boot):
        """Record a worker incarnation; leases held by its earlier incarnations end."""
        known = self.worker_boots.get(worker, -1)
        if boot <= known:
            return
        self.worker_boots[worker] = boot
        self.log("worker_registered", worker=worker, boot=boot)
        for lease in self.leases.held_by(worker):
            if lease.boot is None or lease.boot >= boot:
                continue
            job = self.store.get(lease.key)
            self.leases.drop(lease.key)
            if job is None or job["state"] != models.LEASED or job["epoch"] != lease.epoch:
                continue
            job["state"] = models.QUEUED
            job["granted"] = False
            self.store.put(job)
            self.metrics.incr("revocations")
            self.log("lease_revoked", key=job["key"], epoch=lease.epoch, holder=worker,
                     holder_boot=lease.boot, boot=boot)

    @handler("acquire")
    async def acquire(self, src, p):
        req_id = str(p.get("req_id", ""))
        boot = int(p.get("boot", 0))
        known = self.worker_boots.get(src, -1)
        if boot < known:
            raise RpcError("STALE_BOOT", f"{src} boot {boot} < {known}")
        if boot > known:
            self._note_boot(src, boot)
        cached = self.acquire_cache.get(src)
        if cached is not None and cached[0] == req_id:
            reply = cached[1]
            grant = reply.get("lease")
            if grant is None:
                return reply
            job = self.store.get(grant["key"])
            if (job is not None and job["state"] == models.LEASED
                    and job["epoch"] == grant["epoch"] and job["holder"] == src):
                self.log("acquire_duplicate", worker=src, req_id=req_id, key=grant["key"],
                         epoch=grant["epoch"])
                return reply
        job = self.dispatcher.next_job()
        if job is None:
            reply = {"lease": None}
        else:
            reply = {"lease": self._grant(job, src, boot)}
        self.acquire_cache[src] = (req_id, reply)
        return reply

    def _grant(self, job, worker, boot):
        op_id = models.current_op_id(job)
        if op_id not in job["ops"]:
            self.capacity.reserve(job, op_id)
        job["epoch"] += 1
        job["state"] = models.LEASED
        job["holder"] = worker
        job["holder_boot"] = boot
        job["granted"] = True
        mode = models.MODE_CANCEL if job["cancel"] else models.MODE_RUN
        self.store.put(job)
        lease = self.leases.grant(job["key"], job["epoch"], worker, boot)
        self.metrics.incr("grants")
        self.log("lease_granted", key=job["key"], epoch=job["epoch"], holder=worker,
                 holder_boot=boot, op_id=op_id, attempt=job["attempt"], mode=mode,
                 lease_s=lease.lease_s)
        return {"key": job["key"], "tenant": job["tenant"], "job_id": job["job_id"],
                "units": job["units"], "duration": job["duration"], "attempt": job["attempt"],
                "epoch": job["epoch"], "op_id": op_id, "mode": mode,
                "lease_s": lease.lease_s, "expires_at": lease.expires_at()}

    def _check_holder(self, src, key, epoch, boot):
        """The job, if (src, epoch, boot) is its current lease; otherwise FENCED."""
        job = self.store.get(key)
        if job is None:
            raise RpcError("FENCED", "unknown job")
        if (job["epoch"] != epoch or job["state"] != models.LEASED or job["holder"] != src
                or job["holder_boot"] != boot):
            self.metrics.incr("fenced")
            self.log("lease_fenced", key=key, epoch=epoch, current=job["epoch"], holder=src,
                     state=job["state"])
            raise RpcError("FENCED", f"epoch {epoch} is not the current lease")
        return job

    @handler("renew")
    async def renew(self, src, p):
        key, epoch, boot = models.parse_lease_ref(p)
        job = self._check_holder(src, key, epoch, boot)
        lease = self.leases.get(key)
        if lease is None or lease.epoch != epoch:
            raise RpcError("FENCED", "no live lease")
        if not self.leases.is_live(lease):
            self.log("lease_renew_rejected", key=key, epoch=epoch, holder=src)
            raise RpcError("EXPIRED", "lease expired")
        self.leases.renew(lease)
        self.metrics.incr("renewals")
        self.log("lease_renewed", key=key, epoch=epoch, holder=src)
        return {"ok": True, "cancel": bool(job["cancel"]), "expires_at": lease.expires_at()}

    @handler("report")
    async def report(self, src, p):
        key, epoch, boot = models.parse_lease_ref(p)
        op_id = str(p.get("op_id", ""))
        state = p.get("state")
        job = self.store.get(key)
        if job is None:
            raise RpcError("FENCED", "unknown job")
        last = job["last_report"]
        if last is not None and last["epoch"] == epoch and last["op_id"] == op_id:
            self.log("report_duplicate", key=key, epoch=epoch, op_id=op_id)
            return {"ok": True, "state": job["state"]}
        if models.is_terminal(job) or job["epoch"] != epoch or job["holder"] != src \
                or job["holder_boot"] != boot:
            self.metrics.incr("fenced")
            self.log("lease_fenced", key=key, epoch=epoch, current=job["epoch"], holder=src,
                     state=job["state"])
            raise RpcError("FENCED", f"epoch {epoch} is not the current lease")
        if op_id != models.current_op_id(job) or state not in models.OP_TERMINAL:
            raise RpcError("INVALID", "report must name the current attempt's terminal op")
        self._apply_report(job, epoch, op_id, state, p.get("result"))
        return {"ok": True, "state": job["state"]}

    def _apply_report(self, job, epoch, op_id, state, result):
        op = job["ops"][op_id]
        op["outcome"] = state
        op["rel"] = models.REL_RELEASING
        job["last_report"] = {"epoch": epoch, "op_id": op_id, "state": state}
        job["holder"] = None
        job["holder_boot"] = None
        if state == "SUCCEEDED":
            job["state"] = models.SUCCEEDED
            job["result"] = result
            job["final_op"] = op_id
        elif state == "CANCELLED" or job["cancel"]:
            job["state"] = models.CANCELLED
        elif job["attempt"] >= config.MAX_GPU_ATTEMPTS:
            job["state"] = models.FAILED
        else:
            job["attempt"] += 1
            job["granted"] = False
            job["state"] = models.QUEUED
        self.store.put(job)
        self.leases.drop(job["key"])
        self.metrics.incr("reports")
        self.log("report_applied", key=job["key"], epoch=epoch, op_id=op_id, op_state=state,
                 state=job["state"], attempt=job["attempt"])
        if models.is_terminal(job):
            self._log_terminal(job, reason="report")
        self.releaser.start(job["key"], op_id)

    # =====================================================================================
    # GPU notifications and background work
    # =====================================================================================

    @handler("release_complete")
    async def release_complete(self, src, p):
        op_id = p.get("op_id")
        if isinstance(op_id, str):
            self.releaser.notify_complete(op_id)
        return {}

    def _released(self, key, op_id):
        job = self.store.get(key)
        if job is None:
            return
        if self.capacity.mark_freed(job, op_id):
            self.store.put(job)
            self.metrics.incr("releases")

    async def _lease_monitor(self):
        """Return jobs whose leases ran out (plus the re-grant wait) to the queue."""
        while True:
            await self.sleep(config.LEASE_SCAN_S)
            for lease in self.leases.due_for_regrant():
                job = self.store.get(lease.key)
                self.leases.drop(lease.key)
                if job is None or job["state"] != models.LEASED or job["epoch"] != lease.epoch:
                    continue
                job["state"] = models.QUEUED
                job["granted"] = False
                self.store.put(job)
                self.metrics.incr("expiries")
                self.log("lease_expired", key=job["key"], epoch=lease.epoch, holder=lease.holder,
                         cancel=job["cancel"])

    @handler("stats")
    async def stats(self, src, p):
        return self.metrics.snapshot()

    def _log_terminal(self, job, reason):
        self.log("job_terminal", key=job["key"], state=job["state"], op_id=job["final_op"],
                 attempt=job["attempt"], reason=reason)
