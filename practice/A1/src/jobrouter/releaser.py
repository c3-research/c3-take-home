"""Release of finished GPU operations (router side).

Once a job attempt's operation is terminal and the router has recorded its
outcome, the router releases the operation and returns its units to the
capacity ledger. Release is asynchronous on the GPU: the ``release`` reply only
means the request was recorded. The units are counted as free only after
ReleaseComplete has been observed, either as a ``release_complete`` message
(sent to the router, which issues every release) or as ``released: true`` in
``status``. The message is a hint that can be lost or duplicated; the status
poll every ``RELEASE_POLL_S`` is the fallback.

Release tasks are restarted on boot for every operation the job records mark as
``releasing``.
"""

from c3sim import RpcError

from . import config
from .gpu_client import GpuClient
from .retry import Backoff


class ReleaseManager:
    """One background task per operation being released."""

    def __init__(self, node, on_freed):
        self._node = node
        self._gpu = GpuClient(node)
        self._on_freed = on_freed
        self._active = {}
        self._completed = set()
        self._waiters = {}

    def start(self, key, op_id):
        """Ensure a release task is running for `op_id` (idempotent)."""
        if op_id in self._active:
            return
        self._active[op_id] = key
        self._node.spawn(self._run(key, op_id))

    def notify_complete(self, op_id):
        """A release_complete message arrived for `op_id`."""
        if op_id not in self._active:
            return
        self._completed.add(op_id)
        fut = self._waiters.pop(op_id, None)
        if fut is not None:
            fut.set_result(True)

    def pending(self):
        return sorted(self._active)

    def __len__(self):
        return len(self._active)

    async def _wait_hint(self, op_id, seconds):
        """Wait up to `seconds` for a release_complete message. True if one arrived."""
        if op_id in self._completed:
            return True
        node = self._node
        fut = node.future()
        self._waiters[op_id] = fut

        async def timer():
            await node.sleep(seconds)
            fut.set_result(False)

        t = node.spawn(timer())
        got = await fut
        t.cancel()
        if self._waiters.get(op_id) is fut:
            del self._waiters[op_id]
        return bool(got) or op_id in self._completed

    async def _request(self, key, op_id):
        node = self._node
        backoff = Backoff(node.rng)
        while True:
            code = await self._gpu.release(op_id)
            if code is None:
                node.log("release_requested", key=key, op_id=op_id)
                return
            node.log("release_retry", key=key, op_id=op_id, reason=code)
            await node.sleep(backoff.next())

    async def _run(self, key, op_id):
        node = self._node
        await self._request(key, op_id)
        polls = 0
        via = None
        while via is None:
            if await self._wait_hint(op_id, config.RELEASE_POLL_S):
                via = "message"
                break
            polls += 1
            try:
                st = await self._gpu.status(op_id)
            except RpcError:
                st = None
            if st is not None and st.get("released"):
                via = "status"
        node.log("release_observed", key=key, op_id=op_id, via=via, polls=polls)
        self._active.pop(op_id, None)
        self._completed.discard(op_id)
        self._on_freed(key, op_id)
