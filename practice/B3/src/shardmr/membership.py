"""Worker membership: registration, heartbeats, failure detection, epochs.

Every registration of a worker is issued a strictly larger *worker epoch*,
persisted on the coordinator before it is returned. Assignments, commits and
reports carry the epoch they were issued under, and the coordinator rejects
any whose epoch is not the worker's current one. Declaring a worker dead
advances its epoch, so a worker that was only paused is fenced when it
resumes and must register again.
"""

from c3sim import RpcError

from . import config


class WorkerInfo:
    """The coordinator's record of one worker, kept for the coordinator's lifetime.

    Each registration moves the record to the epoch it was issued; heartbeat
    ordering (`last_hb_seq`) belongs to the worker and is kept across them.
    """

    __slots__ = ("name", "epoch", "boot", "reg_seq", "alive", "last_hb", "last_hb_seq")

    def __init__(self, name, now):
        self.name = name
        self.epoch = 0
        self.boot = -1
        self.reg_seq = -1
        self.alive = False
        self.last_hb = now
        self.last_hb_seq = -1

    def admit(self, epoch, boot, reg_seq, now):
        self.epoch = epoch
        self.boot = boot
        self.reg_seq = reg_seq
        self.alive = True
        self.last_hb = now


class Membership:
    """The coordinator's view of its workers (in memory, epochs on disk)."""

    def __init__(self, node, store):
        self.node = node
        self.store = store
        self.workers = {}

    def _persisted(self, name):
        return self.store.get_worker(name) or {"epoch": 0, "boot": -1, "reg_seq": -1}

    def register(self, name, boot, reg_seq):
        """Register (or re-register) a worker incarnation. Returns (epoch, fresh)."""
        rec = self._persisted(name)
        nonce, have = (int(boot), int(reg_seq)), (int(rec["boot"]), int(rec["reg_seq"]))
        info = self.workers.get(name)
        if nonce < have:
            raise RpcError("STALE_REGISTER", f"{name} registration {nonce} < {have}")
        if nonce == have and info is not None and info.alive and info.epoch == rec["epoch"]:
            return info.epoch, False
        epoch = int(rec["epoch"]) + 1
        self.store.put_worker(name, {"epoch": epoch, "boot": nonce[0], "reg_seq": nonce[1]})
        if info is None:
            info = self.workers[name] = WorkerInfo(name, self.node.clock.now())
        info.admit(epoch, nonce[0], nonce[1], self.node.clock.now())
        self.node.log("worker_registered", worker=name, epoch=epoch, boot=nonce[0],
                      reg_seq=nonce[1])
        return epoch, True

    def is_current(self, name, epoch):
        info = self.workers.get(name)
        return info is not None and info.alive and info.epoch == int(epoch)

    def heartbeat(self, name, epoch, hb_seq, sent=None):
        """Record a heartbeat. Returns the WorkerInfo, or None if the sender is fenced.

        Returns (info, fresh): `fresh` is False for a heartbeat older than one
        already processed, and for one that arrives after the worker has
        already given up waiting for its reply (its running list is then older
        than the one the worker sends next).
        """
        if not self.is_current(name, epoch):
            return None, False
        info = self.workers[name]
        if int(hb_seq) <= info.last_hb_seq:
            return info, False
        if sent is not None and self.node.clock.now() - float(sent) > config.HEARTBEAT_RPC_TIMEOUT_S:
            return info, False
        info.last_hb_seq = int(hb_seq)
        info.last_hb = self.node.clock.now()
        return info, True

    def dispatchable(self):
        return [self.workers[n] for n in sorted(self.workers) if self.workers[n].alive]

    def expire(self):
        """Declare dead every worker silent for longer than the dead-after budget."""
        now = self.node.clock.now()
        dead_after = config.worker_dead_after()
        out = []
        for name in sorted(self.workers):
            info = self.workers[name]
            if not info.alive or now - info.last_hb <= dead_after:
                continue
            self.fence(name, "heartbeat_timeout", silence=round(now - info.last_hb, 6),
                       dead_after=dead_after)
            out.append(name)
        return out

    def fence(self, name, reason, **fields):
        """Advance a worker's epoch so everything it holds is revoked."""
        info = self.workers.get(name)
        rec = self._persisted(name)
        epoch = int(rec["epoch"]) + 1
        rec["epoch"] = epoch
        self.store.put_worker(name, rec)
        if info is not None:
            info.alive = False
            info.epoch = epoch
        self.node.log("worker_dead", worker=name, epoch=epoch, reason=reason, **fields)
        return epoch
