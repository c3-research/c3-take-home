"""Running one lease on a worker.

`LeaseExecutor` carries a job attempt from grant to report:

1. make sure the attempt's GPU operation exists (idempotent submit with
   poll-before-resubmit, `GpuClient.submit`);
2. poll its status until it is terminal, cancelling it first if the lease is
   in cancel mode or a renew reply says a cancel was requested;
3. report the terminal state to the router under the lease's epoch.

A renew task runs alongside, extends the holder's view of the lease and ends
the lease once it runs out. The holder checks its lease before every protected
action (submit, cancel, report). When the lease is no longer valid, or the
router fences it, the executor abandons the job without acting further; the
next holder of the job adopts the same operation through the same ``op_id``.
"""

from c3sim import RpcError, RpcTimeout

from . import config, models
from .gpu_client import GpuClient, LeaseLost
from .retry import Backoff


class LeaseExecutor:
    def __init__(self, worker, lease):
        self.w = worker
        self.lease = lease
        self.gpu = GpuClient(worker)

    async def run(self):
        w, lease = self.w, self.lease
        w.log("lease_acquired", key=lease.key, epoch=lease.epoch, op_id=lease.op_id,
              mode=lease.mode, attempt=lease.attempt, valid_for=round(lease.remaining(), 6))
        renewer = w.spawn(self._renew_loop())
        try:
            outcome = await self._execute()
        except LeaseLost as e:
            lease.lose(lease.lost or "expired")
            outcome = "abandoned"
            w.log("lease_abandoned", key=lease.key, epoch=lease.epoch, op_id=lease.op_id,
                  step=str(e), reason=lease.lost)
        renewer.cancel()
        return outcome

    # --- main path --------------------------------------------------------------------
    async def _execute(self):
        w, lease = self.w, self.lease
        st = await self.gpu.submit(lease)
        state = st["state"]
        result = st.get("result")
        cancel_sent = False
        while state not in models.OP_TERMINAL:
            if lease.cancel_requested and not cancel_sent:
                cancel_sent = await self.gpu.cancel(lease)
                if cancel_sent:
                    w.log("gpu_cancel_sent", key=lease.key, op_id=lease.op_id,
                          epoch=lease.epoch)
            await w.sleep(config.GPU_POLL_S)
            if not lease.valid():
                raise LeaseLost("poll")
            s = await self.gpu.status(lease.op_id)
            if s is None or s.get("state") is None:
                continue
            if s["state"] != state:
                w.log("gpu_op_state", key=lease.key, op_id=lease.op_id, state=s["state"])
            state = s["state"]
            result = s.get("result")
        return await self._report(state, result)

    async def _report(self, state, result):
        w, lease = self.w, self.lease
        body = {"key": lease.key, "epoch": lease.epoch, "boot": w.boot_count,
                "op_id": lease.op_id, "state": state}
        if result is not None:
            body["result"] = result
        backoff = Backoff(w.rng, base=config.GPU_BACKOFF_BASE_S, cap=config.LEASE_S / 4)
        while lease.lost is None:
            w.log("job_report", key=lease.key, epoch=lease.epoch, op_id=lease.op_id,
                  state=state)
            try:
                r = await w.rpc(w.router, "report", body, config.REPORT_TIMEOUT_S)
                w.log("job_reported", key=lease.key, epoch=lease.epoch, op_id=lease.op_id,
                      state=state, job_state=r.get("state"))
                lease.lose("reported")
                return "reported"
            except RpcTimeout:
                w.log("job_report_retry", key=lease.key, epoch=lease.epoch, reason="timeout")
            except RpcError as e:
                if e.code == "FENCED":
                    lease.lose("fenced")
                    w.log("lease_fenced", key=lease.key, epoch=lease.epoch, step="report")
                    return "fenced"
                w.log("job_report_retry", key=lease.key, epoch=lease.epoch, reason=e.code)
            await w.sleep(backoff.next())
        raise LeaseLost("report")

    # --- renewals ---------------------------------------------------------------------
    async def _renew_loop(self):
        w, lease = self.w, self.lease
        while True:
            await w.sleep(config.RENEW_INTERVAL_S)
            if not lease.valid():
                if lease.lost is None:
                    lease.lose("expired")
                    w.log("lease_expired_local", key=lease.key, epoch=lease.epoch)
                return
            sent_at = w.clock.now()
            try:
                r = await w.rpc(w.router, "renew", lease.ref(w.boot_count),
                                config.RENEW_TIMEOUT_S)
            except RpcTimeout:
                w.log("lease_renew_timeout", key=lease.key, epoch=lease.epoch)
                continue
            except RpcError as e:
                if e.code in ("FENCED", "EXPIRED"):
                    lease.lose(e.code.lower())
                    w.log("lease_lost", key=lease.key, epoch=lease.epoch, reason=e.code)
                    return
                w.log("lease_renew_error", key=lease.key, epoch=lease.epoch, code=e.code)
                continue
            if lease.lost is not None:
                return
            lease.extend(sent_at)
            if r.get("cancel") and not lease.cancel_requested:
                lease.cancel_requested = True
                w.log("cancel_observed", key=lease.key, epoch=lease.epoch, op_id=lease.op_id)
