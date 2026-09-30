"""Durable coordinator state: job records, worker records, generation counter.

Each job lives under one disk key (`job/<tenant>:<job_id>`), so every job state
transition is a single atomic write. Shard commits are kept separately by
`commit.CommitLog`.
"""

from . import ids

# Job states. Terminal states never change once written.
PENDING = "PENDING"
MAPPING = "MAPPING"
REDUCING = "REDUCING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
TERMINAL = (SUCCEEDED, FAILED, CANCELLED)

SPEC_FIELDS = ("shards", "records", "units", "duration")


def new_job(tenant, job_id, spec):
    return {"jkey": ids.job_key(tenant, job_id), "tenant": tenant, "job_id": job_id,
            "spec": spec, "state": PENDING, "attempt": 0, "reduce_try": 0,
            "reduce_op": None, "open_ops": [], "result": None, "reason": None}


def tombstone(tenant, job_id):
    """A cancel recorded before the job it names was submitted."""
    rec = new_job(tenant, job_id, None)
    rec["state"] = CANCELLED
    rec["reason"] = "cancelled_before_submit"
    return rec


class JobStore:
    """Thin typed layer over the coordinator's disk."""

    def __init__(self, disk):
        self.disk = disk

    # --- generation ------------------------------------------------------------

    def next_generation(self):
        gen = int(self.disk.get(ids.GEN_KEY, 0)) + 1
        self.disk.put(ids.GEN_KEY, gen)
        return gen

    # --- jobs --------------------------------------------------------------------

    def get(self, jkey):
        return self.disk.get(ids.job_record_key(jkey))

    def put(self, rec):
        self.disk.put(ids.job_record_key(rec["jkey"]), rec)

    def job_keys(self):
        n = len(ids.JOB_PREFIX)
        return [k[n:] for k in self.disk.keys(ids.JOB_PREFIX)]

    def add_open_op(self, rec, op_id):
        if op_id not in rec["open_ops"]:
            rec["open_ops"] = sorted(rec["open_ops"] + [op_id])

    def drop_open_op(self, jkey, op_id):
        rec = self.get(jkey)
        if rec is not None and op_id in rec["open_ops"]:
            rec["open_ops"] = [o for o in rec["open_ops"] if o != op_id]
            self.put(rec)

    # --- workers -------------------------------------------------------------------

    def get_worker(self, name):
        return self.disk.get(ids.worker_record_key(name))

    def put_worker(self, name, rec):
        self.disk.put(ids.worker_record_key(name), rec)


def spec_matches(rec, spec):
    """True if a repeated submit names the same work as the stored job."""
    if rec.get("spec") is None:
        return True
    return all(rec["spec"].get(f) == spec.get(f) for f in SPEC_FIELDS)
