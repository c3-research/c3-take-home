"""Durable job table for the router.

`JobStore` mirrors every job record in memory and writes each change to the
node's disk before the caller acts on it. One job is one disk key
(``job:<tenant>/<job_id>``), so a record is always internally consistent after
a crash; the runtime has no multi-key transactions and none are needed.

The store also keeps the dispatch queue: QUEUED jobs ordered by submission
sequence number. The queue is derived state and is rebuilt from the records on
every boot.
"""

import bisect

from . import models

PREFIX = "job:"


class JobStore:
    """In-memory mirror of the router's durable job records."""

    def __init__(self, node):
        self._node = node
        self._disk = node.disk
        self.jobs = {}
        self._queue = []
        self._next_seq = 1

    # --- loading ------------------------------------------------------------------
    def load(self):
        """Read every record from disk and rebuild the queue. Returns the job count."""
        self.jobs = {}
        self._queue = []
        top = 0
        for dk in self._disk.keys(PREFIX):
            job = self._disk.get(dk)
            if job is None:
                continue
            self.jobs[job["key"]] = job
            top = max(top, int(job["seq"]))
            if job["state"] == models.QUEUED:
                self._enqueue(job)
        self._next_seq = top + 1
        return len(self.jobs)

    # --- access -------------------------------------------------------------------
    def get(self, key):
        return self.jobs.get(key)

    def lookup(self, tenant, job_id):
        return self.jobs.get(models.job_key(tenant, job_id))

    def ordered(self):
        """All jobs in submission order."""
        return sorted(self.jobs.values(), key=lambda j: j["seq"])

    def in_state(self, state):
        return [j for j in self.ordered() if j["state"] == state]

    def next_seq(self):
        s = self._next_seq
        self._next_seq += 1
        return s

    # --- writes -------------------------------------------------------------------
    def put(self, job):
        """Persist `job` (durable on return) and update the queue."""
        self._disk.put(PREFIX + job["key"], job)
        self.jobs[job["key"]] = job
        self._dequeue(job["key"], job["seq"])
        if job["state"] == models.QUEUED:
            self._enqueue(job)

    # --- queue --------------------------------------------------------------------
    def _enqueue(self, job):
        item = (int(job["seq"]), job["key"])
        i = bisect.bisect_left(self._queue, item)
        if i < len(self._queue) and self._queue[i] == item:
            return
        self._queue.insert(i, item)

    def _dequeue(self, key, seq):
        item = (int(seq), key)
        i = bisect.bisect_left(self._queue, item)
        if i < len(self._queue) and self._queue[i] == item:
            del self._queue[i]

    def queued(self):
        """QUEUED jobs in dispatch (submission) order."""
        return [self.jobs[k] for _, k in self._queue]

    def queue_depth(self):
        return len(self._queue)
