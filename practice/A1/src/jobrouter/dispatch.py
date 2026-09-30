"""Dispatch policy: which queued job the router leases next.

Jobs are dispatched in submission order. A job whose attempt already owns a
capacity reservation (a re-grant after a lease ran out, including a job being
cancelled after its first grant) is always dispatchable, because granting it
reserves nothing new. Any other job needs its units to fit in the ledger's free
capacity.

Head-of-line reservation keeps large jobs from starving: when the oldest job
that needs fresh capacity does not fit, its units are held back, and a younger
job is only dispatched from the capacity left over beyond them. Once enough
reservations are freed, the blocked job is dispatched ahead of the jobs behind
it. Only one job is held back at a time, and only for `HOL_HOLD_S` of router
time: after that the policy falls back to first-fit, so a single oversized
job cannot stall the queue.
"""

from . import config, models


class Dispatcher:
    def __init__(self, node, store, capacity):
        self._node = node
        self._store = store
        self._capacity = capacity
        self._blocked_key = None
        self._blocked_since = None

    @staticmethod
    def needs_capacity(job):
        """Whether granting `job` would reserve new units."""
        return models.current_op_id(job) not in job["ops"]

    def _hold(self, job):
        """Start (or continue) holding back capacity for `job`. Returns the units held."""
        now = self._node.clock.now()
        if self._blocked_key != job["key"]:
            self._blocked_key = job["key"]
            self._blocked_since = now
            self._node.log("dispatch_blocked", key=job["key"], units=job["units"],
                           free=self._capacity.free())
        if now - self._blocked_since > config.HOL_HOLD_S:
            return 0
        return int(job["units"])

    def _clear(self, key):
        if self._blocked_key == key:
            self._blocked_key = None
            self._blocked_since = None

    def next_job(self):
        """The next job to lease, or None."""
        held = 0
        head_seen = False
        for job in self._store.queued():
            if not self.needs_capacity(job):
                return job
            units = int(job["units"])
            if units + held <= self._capacity.free():
                self._clear(job["key"])
                return job
            if not head_seen:
                head_seen = True
                held = self._hold(job)
        return None

    def forget(self, key):
        """`key` left the queue for a reason other than dispatch (cancel, terminal)."""
        self._clear(key)

    def snapshot(self):
        return {"queued": self._store.queue_depth(), "blocked": self._blocked_key}
