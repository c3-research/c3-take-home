"""Execution of one map task on a worker.

A task runs as exactly one GPU operation, whose op_id is derived from the task
id and recorded on the worker's disk before the first submit. When the
operation succeeds the worker computes the shard output and offers it to the
coordinator's commit log, carrying the worker epoch the task was assigned
under. Whatever happens, the operation is released and its release waited
out before the task reports its outcome.

Task outcomes reported to the coordinator:

    committed       this task's output is the shard's committed output
    duplicate       another task committed the shard first
    failed          the GPU operation ended FAILED
    aborted         the task was aborted (cancelled, or never submitted)
    fenced          the coordinator rejected the commit (stale epoch or task)
    stale           the commit named an abandoned job attempt
    commit_unknown  no commit reply arrived within COMMIT_MAX_ATTEMPTS
"""

from c3sim import RpcError, RpcTimeout

from . import config, ids, kernels

_REJECT_OUTCOME = {"FENCED": "fenced", "REVOKED": "fenced", "UNKNOWN_TASK": "fenced",
                   "STALE_ATTEMPT": "stale"}


class TaskRunner:
    def __init__(self, worker, desc, epoch, batch):
        self.w = worker
        self.desc = desc
        self.tid = desc["task"]
        self.epoch = epoch
        self.batch = batch
        self.op_id = ids.map_op_id(self.tid)
        self.aborted = False
        self.outcome = None

    def abort(self):
        self.aborted = True

    def _stopped(self):
        return self.aborted

    async def run(self):
        w, d = self.w, self.desc
        if self.aborted:
            self.outcome = "aborted"
            return self.outcome
        key = ids.worker_op_key(self.op_id)
        w.disk.put(key, {"task": self.tid, "batch": self.batch, "epoch": self.epoch})
        w.log("task_start", task=self.tid, op_id=self.op_id, shard=d["shard"],
              speculative=d.get("speculative", False))
        payload = kernels.map_payload(d["job"], d["attempt"], d["shard"], self.tid, d["records"])
        r = await w.gpu.submit(self.op_id, d["tenant"], d["units"], d["duration"], payload,
                               should_stop=self._stopped)
        if r is None:
            w.disk.delete(key)
            self.outcome = "aborted"
            return self.outcome
        st = await w.gpu.wait_terminal(self.op_id, should_stop=self._stopped)
        self.outcome = await self._conclude(st)
        await w.gpu.release(self.op_id)
        w.disk.delete(key)
        w.log("task_done", task=self.tid, op_id=self.op_id, outcome=self.outcome)
        return self.outcome

    async def _conclude(self, st):
        if st["state"] == "FAILED":
            return "failed"
        if st["state"] != "SUCCEEDED":
            return "aborted"
        result = st.get("result") or {}
        if result.get("op_id") != self.op_id:
            self.w.log("task_result_mismatch", task=self.tid, op_id=self.op_id)
            return "failed"
        output = kernels.map_output(result["payload"])
        if self.aborted:
            return "aborted"
        return await self._commit(output)

    async def _commit(self, output):
        w, d = self.w, self.desc
        req = {"worker": w.name, "epoch": self.epoch, "task": self.tid, "job": d["job"],
               "attempt": d["attempt"], "shard": d["shard"], "output": output}
        for i in range(config.COMMIT_MAX_ATTEMPTS):
            if i:
                await w.sleep(config.COMMIT_BACKOFF_S * i)
            try:
                r = await w.rpc(w.coordinator, "commit", req, config.COMMIT_RPC_TIMEOUT_S)
            except RpcTimeout:
                w.log("task_commit_retry", task=self.tid, attempt=i + 1)
                continue
            except RpcError as e:
                w.log("task_commit_rejected", task=self.tid, code=e.code)
                return _REJECT_OUTCOME.get(e.code, "fenced")
            if r.get("committed"):
                w.log("task_committed", task=self.tid, shard=d["shard"])
                return "committed"
            w.log("task_commit_lost", task=self.tid, winner=r.get("winner"))
            return "duplicate"
        return "commit_unknown"
