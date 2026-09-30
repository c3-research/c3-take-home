"""Router metrics: counters, a periodic stats line and the ``stats`` admin call.

The router counts the protocol events operators watch (submissions, grants,
expiries, fencing rejections, reports, releases) and logs one ``router_stats``
line every `STATS_INTERVAL_S` with the counters and the current queue, lease
and capacity gauges. Counters are in memory and restart from zero on boot; the
gauges are derived from durable state and are exact after recovery.
"""

from . import config

COUNTERS = (
    "submitted", "duplicates", "cancels", "grants", "renewals", "expiries",
    "revocations", "fenced", "reports", "releases",
)


class RouterMetrics:
    def __init__(self, router):
        self._router = router
        self.counts = {name: 0 for name in COUNTERS}

    def incr(self, name, n=1):
        self.counts[name] = self.counts.get(name, 0) + n

    def gauges(self):
        r = self._router
        by_state = {}
        for job in r.store.jobs.values():
            by_state[job["state"]] = by_state.get(job["state"], 0) + 1
        return {
            "jobs": len(r.store.jobs),
            "queued": r.store.queue_depth(),
            "leases": len(r.leases),
            "releasing": len(r.releaser),
            "reserved": r.capacity.reserved(),
            "capacity": r.capacity.capacity,
            "workers": len(r.worker_boots),
            "states": {k: by_state[k] for k in sorted(by_state)},
        }

    def snapshot(self):
        out = dict(self.gauges())
        out["counts"] = {k: self.counts[k] for k in sorted(self.counts)}
        out["boot"] = self._router.boot_count
        return out

    async def loop(self):
        """Log a stats line periodically. Runs for the life of the incarnation."""
        r = self._router
        while True:
            await r.sleep(config.STATS_INTERVAL_S)
            g = self.gauges()
            r.log("router_stats", queued=g["queued"], leases=g["leases"],
                  releasing=g["releasing"], reserved=g["reserved"], capacity=g["capacity"],
                  jobs=g["jobs"], **{k: self.counts[k] for k in sorted(self.counts)})
