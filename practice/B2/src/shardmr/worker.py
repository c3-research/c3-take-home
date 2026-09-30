"""The shardmr worker node.

A worker registers with the coordinator (receiving a worker epoch), sends
heartbeats listing the tasks it holds, runs the batches of map tasks it is
assigned, and reports each batch once all of its tasks are finished. On
restart it first releases every GPU operation recorded on its disk by the
previous incarnation, then registers afresh; nothing from before the restart
is resumed.
"""

from c3sim import Node, RpcError, RpcTimeout, handler

from . import config, ids
from .executor import TaskRunner
from .gpu_client import GpuClient


class Batch:
    __slots__ = ("bid", "epoch", "runners", "accepted")

    def __init__(self, bid, epoch, runners):
        self.bid = bid
        self.epoch = epoch
        self.runners = runners
        self.accepted = [r.tid for r in runners]


class Worker(Node):
    async def on_start(self):
        self.gpu = GpuClient(self)
        self.coordinator = self.config.get("coordinator", config.COORDINATOR)
        self.epoch = None
        self.reg_seq = 0
        self.hb_seq = 0
        self.tasks = {}
        self.batches = {}
        self.tombstones = set()
        self.log("worker_start", boot=self.boot_count, slots=config.WORKER_SLOTS)
        for key in self.disk.keys(ids.OP_PREFIX):
            self.spawn(self._recover_op(key))
        self.spawn(self._main())

    async def _recover_op(self, key):
        op_id = key[len(ids.OP_PREFIX):]
        st = await self.gpu.settled_absent(op_id)
        if st is not None:
            st = await self.gpu.finish(op_id)
        self.disk.delete(key)
        self.log("recovery_op", op_id=op_id, state=None if st is None else st["state"])

    # --- registration and heartbeats -------------------------------------------------

    async def _register(self):
        self.epoch = None
        self.reg_seq += 1
        while True:
            payload = {"worker": self.name, "boot": self.boot_count, "reg_seq": self.reg_seq}
            try:
                r = await self.rpc(self.coordinator, "register", payload,
                                   config.REGISTER_RPC_TIMEOUT_S)
            except RpcTimeout:
                await self.sleep(config.REGISTER_RETRY_S)
                continue
            except RpcError as e:
                self.log("register_rejected", code=e.code, reg_seq=self.reg_seq)
                self.reg_seq += 1
                await self.sleep(config.REGISTER_RETRY_S)
                continue
            self.epoch = int(r["epoch"])
            self.log("worker_registered", epoch=self.epoch, reg_seq=self.reg_seq, gen=r.get("gen"))
            return

    async def _main(self):
        await self._register()
        sent = self.hb_seq
        while True:
            await self.sleep(config.HEARTBEAT_INTERVAL_S)
            sent += 1
            payload = {"worker": self.name, "epoch": self.epoch, "hb_seq": sent,
                       "running": sorted(self.tasks)}
            try:
                r = await self.rpc(self.coordinator, "heartbeat", payload,
                                   config.HEARTBEAT_RPC_TIMEOUT_S)
            except (RpcTimeout, RpcError):
                continue
            self.hb_seq = sent
            if r.get("reregister"):
                self.log("worker_fenced", epoch=self.epoch, tasks=len(self.tasks))
                self._abort_all()
                await self._register()
                continue
            for tid in r.get("abort", []):
                self._abort(tid, "heartbeat")

    # --- aborts -----------------------------------------------------------------------

    def _abort(self, tid, reason):
        runner = self.tasks.get(tid)
        if runner is None:
            self.tombstones.add(tid)
            return
        if not runner.aborted:
            runner.abort()
            self.log("task_abort", task=tid, reason=reason)

    def _abort_all(self):
        for tid in sorted(self.tasks):
            self._abort(tid, "fenced")

    @handler("abort")
    async def abort(self, src, p):
        for tid in p.get("tasks", []):
            self._abort(str(tid), "coordinator")
        return {"ok": True}

    # --- batches ----------------------------------------------------------------------

    @handler("assign")
    async def assign(self, src, p):
        if self.epoch is None or int(p["epoch"]) != self.epoch:
            raise RpcError("STALE_EPOCH", f"assignment epoch {p['epoch']} != {self.epoch}")
        bid = str(p["batch"])
        known = self.batches.get(bid)
        if known is not None:
            return {"accepted": known.accepted, "hb_seq": self.hb_seq}
        runners = []
        for d in p["tasks"]:
            tid = str(d["task"])
            if tid in self.tombstones or tid in self.tasks:
                continue
            runner = TaskRunner(self, d, self.epoch, bid)
            self.tasks[tid] = runner
            runners.append(runner)
        batch = Batch(bid, self.epoch, runners)
        self.batches[bid] = batch
        self.log("batch_received", batch=bid, tasks=len(runners), epoch=self.epoch)
        self.spawn(self._run_batch(batch))
        return {"accepted": batch.accepted, "hb_seq": self.hb_seq}

    async def _run_batch(self, batch):
        running = [self.spawn(r.run()) for r in batch.runners]
        results = []
        for runner, task in zip(batch.runners, running):
            try:
                outcome = await task
            except RpcError as e:
                self.log("task_error", task=runner.tid, code=e.code)
                outcome = "failed"
            results.append({"task": runner.tid, "outcome": outcome})
        if await self._report(batch, results):
            for runner in batch.runners:
                if self.tasks.get(runner.tid) is runner:
                    del self.tasks[runner.tid]

    async def _report(self, batch, results):
        payload = {"worker": self.name, "epoch": batch.epoch, "batch": batch.bid,
                   "results": results}
        for i in range(config.REPORT_MAX_ATTEMPTS):
            if i:
                await self.sleep(config.REPORT_BACKOFF_S * i)
            try:
                await self.rpc(self.coordinator, "report", payload, config.REPORT_RPC_TIMEOUT_S)
                self.log("batch_reported", batch=batch.bid, tasks=len(results),
                         failed=sum(1 for r in results if r["outcome"] == "failed"))
                return True
            except RpcTimeout:
                continue
            except RpcError as e:
                self.log("batch_report_rejected", batch=batch.bid, code=e.code)
                return False
        self.log("batch_report_abandoned", batch=batch.bid, attempts=config.REPORT_MAX_ATTEMPTS)
        return False

    @handler("release_complete")
    async def release_complete(self, src, p):
        self.gpu.on_release_complete(str(p.get("op_id")))
