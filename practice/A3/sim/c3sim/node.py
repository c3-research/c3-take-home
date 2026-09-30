"""Node base class and the @handler decorator (runtime.md section 2)."""


def handler(method):
    """Mark an `async def name(self, src, payload) -> dict` as the handler for `method`.

    The same handler serves RPCs (its return value is the reply) and one-way
    `send` messages (its return value is ignored). Raise RpcError(code, msg) to
    send an error reply.
    """
    if not isinstance(method, str) or not method:
        raise TypeError("@handler needs a method name string")

    def deco(fn):
        fn._c3sim_handler = method
        return fn
    return deco


class Node:
    """Base class for service nodes. One instance per node incarnation.

    Everything a node may use is on `self`: name, clock, sleep, spawn, rpc,
    send, rng, disk, log, boot_count (plus the extras future() and lock()).
    """

    _c3sim_handlers = {}

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        hs = {}
        for klass in reversed(cls.__mro__):
            for attr, v in vars(klass).items():
                m = getattr(v, "_c3sim_handler", None)
                if isinstance(m, str):
                    hs[m] = attr
        cls._c3sim_handlers = hs

    def __init__(self, ctx):
        self._ctx = ctx

    async def on_start(self):
        """Called at boot and after every restart, before any request is delivered.

        Must return: start long-running loops with self.spawn(...).
        """
        return None

    # --- NodeContext passthroughs -------------------------------------------
    @property
    def ctx(self):
        return self._ctx

    @property
    def name(self):
        return self._ctx.name

    @property
    def clock(self):
        return self._ctx.clock

    @property
    def rng(self):
        return self._ctx.rng

    @property
    def disk(self):
        return self._ctx.disk

    @property
    def config(self):
        """This node's config dict (scenario `service:` merged with the node's `config:`)."""
        return self._ctx.config

    @property
    def boot_count(self):
        return self._ctx.boot_count

    def sleep(self, seconds):
        return self._ctx.sleep(seconds)

    def spawn(self, coro):
        return self._ctx.spawn(coro)

    def rpc(self, dst, method, payload, timeout):
        return self._ctx.rpc(dst, method, payload, timeout)

    def send(self, dst, method, payload):
        return self._ctx.send(dst, method, payload)

    def log(self, event, **fields):
        return self._ctx.log(event, **fields)

    def future(self):
        return self._ctx.future()

    def lock(self):
        return self._ctx.lock()
