"""Worker node: registers with the router, then runs leases in parallel slots.

A worker keeps no durable state. After a restart it holds nothing: it registers
its new incarnation (``hello`` with its boot count), which ends every lease the
router granted to its earlier incarnations, and starts acquiring afresh.

Each slot loops: acquire a lease, run it with `LeaseExecutor`, repeat. The
acquire request carries a request id; retries of a timed-out acquire reuse the
id, so the router can return the lease it already granted, and the lease is
measured from the send time of the *first* attempt with that id.
"""

from c3sim import Node, RpcError, RpcTimeout

from . import config
from .executor import LeaseExecutor
from .leases import HolderLease
from .retry import Backoff, idle_delay


class Worker(Node):
    async def on_start(self):
        self.router = config.router_name(self.config)
        self.slots = config.worker_slots(self.config)
        self.registered = False
        self.running = {}
        self._req_seq = 0
        self.log("worker_start", boot=self.boot_count, slots=self.slots,
                 lease_budget=round(config.holder_budget(), 6), max_drift=config.MAX_DRIFT_S,
                 drift_rate=config.DRIFT_RATE)
        self.spawn(self._main())

    async def _main(self):
        await self._register()
        for slot in range(self.slots):
            self.spawn(self._slot_loop(slot))

    async def _register(self):
        backoff = Backoff(self.rng, base=config.GPU_BACKOFF_BASE_S,
                          cap=config.HELLO_BACKOFF_MAX_S)
        while True:
            try:
                await self.rpc(self.router, "hello", {"boot": self.boot_count},
                               config.HELLO_TIMEOUT_S)
                self.registered = True
                self.log("worker_registered", boot=self.boot_count)
                return
            except (RpcTimeout, RpcError) as e:
                self.log("worker_register_retry", reason=getattr(e, "code", "timeout"))
            await self.sleep(backoff.next())

    async def _slot_loop(self, slot):
        while True:
            lease = await self._acquire(slot)
            if lease is None:
                await self.sleep(idle_delay(self.rng))
                continue
            self.running[slot] = lease.key
            try:
                outcome = await LeaseExecutor(self, lease).run()
            except RpcError as e:
                outcome = "error"
                self.log("lease_error", key=lease.key, epoch=lease.epoch, code=e.code)
            self.running.pop(slot, None)
            self.log("slot_done", slot=slot, key=lease.key, epoch=lease.epoch, outcome=outcome)

    def _next_req_id(self, slot):
        self._req_seq += 1
        return f"{self.name}.b{self.boot_count}.s{slot}.{self._req_seq}"

    async def _acquire(self, slot):
        """One acquire request (with same-id retries). A HolderLease, or None."""
        req_id = self._next_req_id(slot)
        body = {"req_id": req_id, "boot": self.boot_count, "slot": slot}
        sent_at = self.clock.now()
        for attempt in range(config.ACQUIRE_MAX_TRIES):
            try:
                r = await self.rpc(self.router, "acquire", body, config.ACQUIRE_TIMEOUT_S)
            except RpcTimeout:
                self.log("acquire_retry", req_id=req_id, attempt=attempt + 1)
                continue
            except RpcError as e:
                self.log("acquire_rejected", req_id=req_id, code=e.code)
                return None
            grant = r.get("lease")
            if grant is None:
                return None
            lease = HolderLease(self.clock, grant, sent_at)
            if not lease.valid():
                self.log("lease_stale_on_arrival", key=lease.key, epoch=lease.epoch,
                         req_id=req_id)
                return None
            return lease
        return None
