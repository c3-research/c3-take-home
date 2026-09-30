"""Client for the platform GPU service (gpu-api-v3).

`GpuClient` wraps the four GPU methods with the retry discipline the platform
requires:

* ``submit`` is idempotent by ``op_id``. A timeout or ``UNAVAILABLE`` is an
  unknown outcome: the client asks ``status(op_id)`` before submitting again,
  and resubmits only on ``NOT_FOUND``, always with the same ``op_id``.
* ``CAPACITY`` means nothing was created; the same ``op_id`` is retried after
  a backoff.
* Lease-protected calls (submit, cancel) take the caller's `HolderLease` and
  check it immediately before each send, with no await between the check and
  the send. They give up as soon as the lease is no longer valid.

Release is used by the router, which owns capacity: see `releaser.py`.
"""

from c3sim import RpcError, RpcTimeout

from . import config
from .retry import Backoff


class LeaseLost(Exception):
    """The caller's lease stopped being valid before the call could complete."""


class GpuClient:
    def __init__(self, node, gpu=config.GPU_NODE):
        self._node = node
        self.gpu = gpu

    def _rpc(self, method, payload):
        return self._node.rpc(self.gpu, method, payload, config.GPU_RPC_TIMEOUT_S)

    # --- lease-protected ----------------------------------------------------------
    @staticmethod
    def submit_request(lease, holder):
        return {"op_id": lease.op_id, "tenant": lease.tenant, "units": lease.units,
                "duration": lease.duration,
                "payload": {"job": lease.key, "attempt": lease.attempt,
                            "epoch": lease.epoch, "holder": holder}}

    async def submit(self, lease):
        """Make sure the lease's operation exists on the GPU.

        Returns the operation's known state as ``{"state": ..., "result"?}``.
        Raises `LeaseLost` if the lease ran out first.
        """
        node = self._node
        req = self.submit_request(lease, node.name)
        backoff = Backoff(node.rng)
        attempt = 0
        while True:
            if not lease.valid():
                raise LeaseLost("submit")
            attempt += 1
            node.log("gpu_submit", op_id=lease.op_id, key=lease.key, epoch=lease.epoch,
                     attempt=attempt)
            try:
                r = await self._rpc("submit", req)
                state = r.get("state", "PENDING")
                node.log("gpu_submitted", op_id=lease.op_id, state=state, attempt=attempt)
                out = {"state": state}
                if "result" in r:
                    out["result"] = r["result"]
                return out
            except RpcError as e:
                if e.code == "CAPACITY":
                    node.log("gpu_capacity_wait", op_id=lease.op_id, attempt=attempt)
                    await node.sleep(backoff.next())
                    continue
                if e.code != "UNAVAILABLE":
                    raise
                node.log("gpu_submit_unknown", op_id=lease.op_id, reason=e.code,
                         attempt=attempt)
            except RpcTimeout:
                node.log("gpu_submit_unknown", op_id=lease.op_id, reason="timeout",
                         attempt=attempt)
            st = await self.probe(lease, backoff)
            if st is not None:
                node.log("gpu_submitted", op_id=lease.op_id, state=st["state"],
                         attempt=attempt, via="status")
                return st
            await node.sleep(backoff.next())

    async def probe(self, lease, backoff):
        """Resolve an unknown submit outcome: the op's status, or None if NOT_FOUND."""
        node = self._node
        while True:
            if not lease.valid():
                raise LeaseLost("probe")
            try:
                st = await self._rpc("status", {"op_id": lease.op_id})
                return st
            except RpcError as e:
                if e.code == "NOT_FOUND":
                    node.log("gpu_status_probe", op_id=lease.op_id, found=False)
                    return None
                raise
            except RpcTimeout:
                node.log("gpu_status_probe", op_id=lease.op_id, found=None)
                await node.sleep(backoff.next())

    async def cancel(self, lease):
        """Ask the GPU to cancel the lease's operation. True once acknowledged."""
        node = self._node
        if not lease.valid():
            raise LeaseLost("cancel")
        node.log("gpu_cancel", op_id=lease.op_id, key=lease.key, epoch=lease.epoch)
        try:
            await self._rpc("cancel", {"op_id": lease.op_id, "job": lease.key,
                                       "epoch": lease.epoch})
            return True
        except RpcTimeout:
            node.log("gpu_cancel_retry", op_id=lease.op_id, reason="timeout")
            return False
        except RpcError as e:
            node.log("gpu_cancel_retry", op_id=lease.op_id, reason=e.code)
            return False

    # --- read-only ----------------------------------------------------------------
    async def status(self, op_id):
        """Current status of `op_id`, or None if it could not be read this time."""
        try:
            return await self._rpc("status", {"op_id": op_id})
        except RpcTimeout:
            return None
        except RpcError as e:
            if e.code == "NOT_FOUND":
                return {"state": None, "released": False}
            raise

    # --- release (router) -----------------------------------------------------------
    async def release(self, op_id):
        """Request release of `op_id`. Returns the error code, or None when recorded."""
        try:
            await self._rpc("release", {"op_id": op_id})
            return None
        except RpcTimeout:
            return "timeout"
        except RpcError as e:
            return e.code
