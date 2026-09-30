"""Job leases: the grantor's table (router) and the holder's view (worker).

A lease says "worker W alone may act on job J until time T". Each grant of a
job carries an epoch strictly larger than every earlier grant of that job; the
epoch is persisted in the job record before the grant is sent, and every
lease-protected action carries it so that the router can fence stale holders.

Grantor side (`LeaseTable`): lease expiry is kept in memory on the router's own
clock. After a router restart every persisted lease is treated as freshly
granted at boot, because the router cannot know how much of it has elapsed.
A lease is re-granted only after `config.regrant_after()` has passed since its
last acknowledgement.

Holder side (`HolderLease`): the lease interval starts when the holder *sent*
the request that was acknowledged, and is shortened by the drift budget
(`config.holder_budget()`). Timestamps from the router are never compared with
the holder's clock.
"""

from . import config


class RouterLease:
    __slots__ = ("key", "epoch", "holder", "boot", "acked_at", "lease_s")

    def __init__(self, key, epoch, holder, boot, acked_at, lease_s):
        self.key = key
        self.epoch = epoch
        self.holder = holder
        self.boot = boot
        self.acked_at = acked_at
        self.lease_s = lease_s

    def expires_at(self):
        return self.acked_at + self.lease_s

    def regrant_at(self):
        return self.acked_at + config.regrant_after(self.lease_s)


class LeaseTable:
    """Current lease per job, on the router's clock. Not persisted."""

    def __init__(self, clock, lease_s=config.LEASE_S):
        self._clock = clock
        self.lease_s = lease_s
        self._by_key = {}

    def grant(self, key, epoch, holder, boot):
        lease = RouterLease(key, epoch, holder, boot, self._clock.now(), self.lease_s)
        self._by_key[key] = lease
        return lease

    def restore(self, key, epoch, holder, boot):
        """Re-arm a persisted lease after a router restart (full length from now)."""
        return self.grant(key, epoch, holder, boot)

    def get(self, key):
        return self._by_key.get(key)

    def drop(self, key):
        return self._by_key.pop(key, None)

    def is_live(self, lease):
        return self._clock.now() <= lease.expires_at()

    def renew(self, lease):
        lease.acked_at = self._clock.now()
        return lease

    def due_for_regrant(self):
        """Leases whose re-grant wait has passed, in key order."""
        now = self._clock.now()
        return [self._by_key[k] for k in sorted(self._by_key)
                if now >= self._by_key[k].regrant_at()]

    def held_by(self, worker):
        return [self._by_key[k] for k in sorted(self._by_key)
                if self._by_key[k].holder == worker]

    def __len__(self):
        return len(self._by_key)


class HolderLease:
    """A worker's view of one lease it holds."""

    def __init__(self, clock, grant, sent_at):
        self._clock = clock
        self.key = grant["key"]
        self.tenant = grant["tenant"]
        self.job_id = grant["job_id"]
        self.units = int(grant["units"])
        self.duration = float(grant["duration"])
        self.attempt = int(grant["attempt"])
        self.epoch = int(grant["epoch"])
        self.op_id = grant["op_id"]
        self.mode = grant.get("mode", "run")
        self.lease_s = float(grant.get("lease_s", config.LEASE_S))
        self.valid_until = sent_at + config.holder_budget(self.lease_s)
        self.cancel_requested = self.mode == "cancel"
        self.lost = None

    def valid(self):
        """True while this holder may act. Check immediately before every action."""
        return self.lost is None and self._clock.now() < self.valid_until

    def remaining(self):
        return self.valid_until - self._clock.now()

    def extend(self, sent_at):
        """A renew sent at local time `sent_at` was acknowledged."""
        until = sent_at + config.holder_budget(self.lease_s)
        if until > self.valid_until:
            self.valid_until = until

    def lose(self, reason):
        if self.lost is None:
            self.lost = reason

    def ref(self, boot):
        return {"key": self.key, "epoch": self.epoch, "boot": boot}
