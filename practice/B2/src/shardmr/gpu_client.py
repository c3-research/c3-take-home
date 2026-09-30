"""Caller-side driver for the platform GPU service (gpu-api-v3).

`GpuClient` wraps the four GPU methods with the retry discipline the platform
requires: submits are keyed by a caller-chosen `op_id` and repeated with the
same `op_id`; an ambiguous submit (timeout or `UNAVAILABLE`) is resolved with
`status` before anything else is done; completion is learned by polling; and a
release is only considered finished once the GPU reports the units freed.
"""

from c3sim import RpcError, RpcTimeout

from . import config

TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")

_UNKNOWN = object()


class GpuClient:
    """GPU operations issued by one node. One instance per node incarnation."""

    def __init__(self, node, gpu="gpu"):
        self.node = node
        self.gpu = gpu
        self._release_hints = {}

    # --- helpers --------------------------------------------------------------

    async def _call(self, method, payload):
        return await self.node.rpc(self.gpu, method, payload, config.GPU_RPC_TIMEOUT_S)

    async def probe(self, op_id):
        """`status(op_id)`: the status dict, None for NOT_FOUND, _UNKNOWN on timeout."""
        try:
            return await self._call("status", {"op_id": op_id})
        except RpcTimeout:
            return _UNKNOWN
        except RpcError as e:
            if e.code == "NOT_FOUND":
                return None
            raise

    async def _probe_until_known(self, op_id):
        while True:
            st = await self.probe(op_id)
            if st is not _UNKNOWN:
                return st
            await self.node.sleep(config.UNKNOWN_OUTCOME_BACKOFF_S)

    async def settled_absent(self, op_id):
        """Resolve whether an operation whose submit may be in flight exists.

        Returns its status dict if it exists, or None if it is still unknown to
        the GPU after the recovery settle delay.
        """
        st = await self._probe_until_known(op_id)
        if st is not None:
            return st
        await self.node.sleep(config.RECOVERY_SETTLE_S)
        return await self._probe_until_known(op_id)

    # --- submit ---------------------------------------------------------------

    async def submit(self, op_id, tenant, units, duration, payload, should_stop=None):
        """Get `op_id` accepted by the GPU.

        Returns the accepting reply (or a status dict) once the operation
        exists, or None if `should_stop()` became true and the operation was
        never accepted.
        """
        node = self.node
        req = {"op_id": op_id, "tenant": tenant, "units": int(units),
               "duration": float(duration), "payload": payload}
        backoff = config.CAPACITY_BACKOFF_INITIAL_S
        sent = False
        attempt = 0
        while True:
            if should_stop is not None and should_stop():
                if not sent:
                    node.log("gpu_submit_skipped", op_id=op_id)
                    return None
                st = await self.settled_absent(op_id)
                node.log("gpu_submit_abandoned", op_id=op_id, exists=st is not None)
                return st
            attempt += 1
            try:
                r = await self._call("submit", req)
                node.log("gpu_submit", op_id=op_id, units=units, attempt=attempt,
                         state=r.get("state", "PENDING"))
                return r
            except RpcError as e:
                if e.code == "CAPACITY":
                    node.log("gpu_submit_retry", op_id=op_id, code=e.code, attempt=attempt,
                             backoff=backoff)
                    await node.sleep(backoff)
                    backoff = min(backoff * 2.0, config.CAPACITY_BACKOFF_MAX_S)
                    continue
                if e.code != "UNAVAILABLE":
                    node.log("gpu_submit_error", op_id=op_id, code=e.code)
                    raise
                sent = True
                code = e.code
            except RpcTimeout:
                sent = True
                code = "TIMEOUT"
            node.log("gpu_submit_unknown", op_id=op_id, code=code, attempt=attempt)
            wait = config.UNKNOWN_OUTCOME_BACKOFF_S
            while True:
                st = await self._probe_until_known(op_id)
                if st is not None:
                    node.log("gpu_submit_confirmed", op_id=op_id, state=st["state"])
                    return st
                await node.sleep(wait)
                wait = min(wait * 2.0, config.CAPACITY_BACKOFF_MAX_S)

    # --- completion -------------------------------------------------------------

    async def wait_terminal(self, op_id, should_stop=None):
        """Poll until the operation is terminal. Cancels it once `should_stop()`."""
        cancelled = False
        while True:
            if not cancelled and should_stop is not None and should_stop():
                cancelled = True
                await self.cancel(op_id)
            st = await self.probe(op_id)
            if st is not _UNKNOWN and st is not None and st["state"] in TERMINAL:
                self.node.log("gpu_op_terminal", op_id=op_id, state=st["state"])
                return st
            await self.node.sleep(config.GPU_POLL_INTERVAL_S)

    async def cancel(self, op_id):
        """Request cancellation. Returns False if the GPU has no such operation."""
        while True:
            try:
                await self._call("cancel", {"op_id": op_id})
                self.node.log("gpu_cancel", op_id=op_id)
                return True
            except RpcTimeout:
                await self.node.sleep(config.UNKNOWN_OUTCOME_BACKOFF_S)
            except RpcError as e:
                if e.code == "NOT_FOUND":
                    return False
                raise

    # --- release ------------------------------------------------------------------

    def on_release_complete(self, op_id):
        """Handle a `release_complete` hint from the GPU."""
        fut = self._release_hints.get(op_id)
        if fut is not None:
            fut.set_result(True)

    async def _wait_hint(self, fut, timeout):
        if fut.done():
            return
        node = self.node
        woke = node.future()

        async def watch():
            try:
                await fut
            except Exception:  # noqa: BLE001
                pass
            woke.set_result(None)

        async def timer():
            await node.sleep(timeout)
            woke.set_result(None)

        a = node.spawn(watch())
        b = node.spawn(timer())
        await woke
        a.cancel()
        b.cancel()

    async def release(self, op_id):
        """Release a terminal operation and wait until its units are free.

        Returns True once the GPU reports the release complete, False if the
        GPU has no such operation.
        """
        node = self.node
        hint = self._release_hints.get(op_id)
        if hint is None:
            hint = node.future()
            self._release_hints[op_id] = hint
        try:
            while True:
                try:
                    await self._call("release", {"op_id": op_id})
                    node.log("gpu_release", op_id=op_id)
                    break
                except RpcTimeout:
                    await node.sleep(config.UNKNOWN_OUTCOME_BACKOFF_S)
                except RpcError as e:
                    if e.code == "NOT_FOUND":
                        return False
                    if e.code != "NOT_TERMINAL":
                        raise
                    await self.wait_terminal(op_id, should_stop=lambda: True)
            while True:
                if hint.done():
                    node.log("gpu_released", op_id=op_id, via="hint")
                    return True
                st = await self.probe(op_id)
                if st is not _UNKNOWN and st is not None and st.get("released"):
                    node.log("gpu_released", op_id=op_id, via="status")
                    return True
                await self._wait_hint(hint, config.RELEASE_POLL_INTERVAL_S)
        finally:
            self._release_hints.pop(op_id, None)

    async def finish(self, op_id):
        """Drive an operation of unknown state to terminal and release it."""
        st = await self._probe_until_known(op_id)
        if st is None:
            return None
        if st["state"] not in TERMINAL:
            st = await self.wait_terminal(op_id, should_stop=lambda: True)
        await self.release(op_id)
        return st
