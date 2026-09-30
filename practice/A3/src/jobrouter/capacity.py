"""GPU capacity accounting on the router.

The GPU has a fixed capacity and no method to query it, so the router keeps its
own ledger and only grants a lease when the job's units fit. Units are
*reserved* when the first lease for an attempt is granted (before any submit
can reach the GPU) and stay reserved until the router has observed
ReleaseComplete for that attempt's operation: a ``release_complete`` message or
``status(op_id).released == true``. The GPU commits units for exactly a sub-
interval of that window, so the ledger never lets committed units exceed
capacity.

The ledger is derived from the job records (each op carries its units and
release state), so it is rebuilt exactly after a router restart.
"""

from . import models


class CapacityLedger:
    """Reserved GPU units, per operation."""

    def __init__(self, node, capacity):
        self._node = node
        self.capacity = int(capacity)
        self._held = {}

    def rebuild(self, jobs):
        """Recompute reservations from job records (boot)."""
        self._held = {}
        for job in jobs:
            for op_id in sorted(job["ops"]):
                op = job["ops"][op_id]
                if op["rel"] != models.REL_FREED:
                    self._held[op_id] = int(op["units"])
        return self.reserved()

    def reserved(self):
        return sum(self._held.values())

    def free(self):
        return self.capacity - self.reserved()

    def holds(self, op_id):
        return op_id in self._held

    def fits(self, units):
        return int(units) <= self.free()

    def reserve(self, job, op_id):
        """Reserve the job's units for `op_id` and record it on the job (caller persists)."""
        units = int(job["units"])
        job["ops"][op_id] = {"attempt": job["attempt"], "units": units,
                             "rel": models.REL_HELD, "outcome": None}
        self._held[op_id] = units
        self._node.log("capacity_reserved", key=job["key"], op_id=op_id, units=units,
                       reserved=self.reserved(), capacity=self.capacity)

    def mark_freed(self, job, op_id):
        """ReleaseComplete observed for `op_id` (caller persists the job)."""
        op = job["ops"].get(op_id)
        if op is None or op["rel"] == models.REL_FREED:
            return False
        op["rel"] = models.REL_FREED
        self._held.pop(op_id, None)
        self._node.log("capacity_freed", key=job["key"], op_id=op_id, units=op["units"],
                       reserved=self.reserved(), capacity=self.capacity)
        return True

    def snapshot(self):
        return {"capacity": self.capacity, "reserved": self.reserved(),
                "ops": len(self._held)}
