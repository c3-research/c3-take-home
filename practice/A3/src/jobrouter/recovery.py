"""Router boot recovery.

Everything the router knows survives a crash in the job records on disk. On
every boot the router rebuilds its in-memory state from those records, in this
order:

1. load the job table and rebuild the dispatch queue (`JobStore.load`);
2. rebuild the capacity ledger: every operation not yet marked ``freed`` still
   holds its units, including operations whose release was requested before the
   crash (`CapacityLedger.rebuild`);
3. re-arm the lease of every LEASED job. The router cannot know how much of a
   lease elapsed while it was down, so each one is treated as granted at boot
   and runs its full length (plus the re-grant wait) before the job can be
   re-granted. The holder keeps its epoch; renewals and reports under it are
   accepted as before;
4. restart the release task of every job whose operation is marked
   ``releasing``.

Recovery writes nothing: it only re-derives state that the records already
imply, so a crash during recovery is harmless.
"""

from . import models


def recover(router):
    """Rebuild `router`'s in-memory state from disk. Returns a summary dict."""
    store = router.store
    jobs = store.load()
    reserved = router.capacity.rebuild(store.ordered())
    restored = 0
    for job in store.in_state(models.LEASED):
        router.leases.restore(job["key"], job["epoch"], job["holder"], job["holder_boot"])
        restored += 1
        router.log("lease_restored", key=job["key"], epoch=job["epoch"], holder=job["holder"],
                   holder_boot=job["holder_boot"])
    releasing = 0
    for job in store.ordered():
        op_id = models.current_op_id(job)
        op = job["ops"].get(op_id)
        if op is not None and op["rel"] == models.REL_RELEASING:
            router.releaser.start(job["key"], op_id)
            releasing += 1
    summary = {"jobs": jobs, "leased": restored, "releasing": releasing,
               "queued": store.queue_depth(), "reserved": reserved}
    for problem in audit(store.ordered()):
        router.log("recovery_audit", **problem)
    return summary


def audit(jobs):
    """Consistency findings for a set of job records (logged, never repaired)."""
    out = []
    for job in jobs:
        key = job["key"]
        if job["precancelled"] and job["units"] is None:
            continue
        cur = models.current_op_id(job)
        if job["state"] == models.LEASED and job["holder"] is None:
            out.append({"key": key, "finding": "leased_without_holder"})
        if job["state"] == models.QUEUED and cur in job["ops"] and not job["granted"]:
            out.append({"key": key, "finding": "reserved_before_grant"})
        if models.is_terminal(job):
            held = [op for op, rec in sorted(job["ops"].items())
                    if rec["rel"] == models.REL_HELD]
            if held:
                out.append({"key": key, "finding": "terminal_with_held_op",
                            "op_id": held[0]})
        for op_id, rec in sorted(job["ops"].items()):
            if rec["attempt"] > job["attempt"]:
                out.append({"key": key, "finding": "op_from_future_attempt", "op_id": op_id})
    return out
