"""The reduce stage.

Once every shard of a job attempt has committed, the coordinator runs one
reduce operation on the GPU. Its payload is built only from the commit log,
from the attempt that is being reduced, so the reduce reads each shard's
committed output exactly once and never mixes attempts. The reduce op_id is
derived from (job, attempt, reduce try) and recorded in the job's `open_ops`
before the submit, so a restarted coordinator resumes the same operation and
releases it afterwards.
"""

from . import config, ids, kernels
from .store import REDUCING


class ReduceStage:
    def __init__(self, coord):
        self.c = coord
        self.running = set()

    def start(self, jkey):
        if jkey in self.running:
            return
        self.running.add(jkey)
        self.c.spawn(self._run(jkey))

    def _active(self, jkey, attempt):
        rec = self.c.store.get(jkey)
        return rec is not None and rec["state"] == REDUCING and rec["attempt"] == attempt

    async def _run(self, jkey):
        try:
            while True:
                rec = self.c.store.get(jkey)
                if rec is None or rec["state"] != REDUCING:
                    break
                await self._reduce_once(rec)
        finally:
            self.running.discard(jkey)
        await self.c.release_open_ops(jkey)

    async def _reduce_once(self, rec):
        c = self.c
        jkey, attempt = rec["jkey"], int(rec["attempt"])
        shards = int(rec["spec"]["shards"])
        inputs = c.commits.inputs(jkey, attempt)
        if len(inputs) != shards:
            c.log("reduce_incomplete", job=jkey, attempt=attempt, committed=len(inputs),
                  shards=shards)
            c.reopen_mapping(jkey, attempt)
            return
        op_id = ids.reduce_op_id(jkey, attempt, rec["reduce_try"])
        if rec["reduce_op"] != op_id or op_id not in rec["open_ops"]:
            rec["reduce_op"] = op_id
            c.store.add_open_op(rec, op_id)
            c.store.put(rec)
        payload = kernels.reduce_payload(jkey, attempt, shards, inputs)
        c.log("reduce_submit", job=jkey, attempt=attempt, op_id=op_id, inputs=len(inputs))

        def stop():
            return not self._active(jkey, attempt)

        r = await c.gpu.submit(op_id, rec["tenant"], config.REDUCE_UNITS,
                               config.REDUCE_DURATION_S, payload, should_stop=stop)
        if r is None:
            c.store.drop_open_op(jkey, op_id)
            return
        st = await c.gpu.wait_terminal(op_id, should_stop=stop)
        if st["state"] == "SUCCEEDED":
            result = kernels.reduce_output(st["result"]["payload"])
            c.complete_job(jkey, attempt, op_id, result)
        elif st["state"] == "FAILED":
            c.reduce_failed(jkey, attempt, op_id)
        await c.gpu.release(op_id)
        c.store.drop_open_op(jkey, op_id)
