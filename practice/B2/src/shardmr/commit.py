"""Shard output commit log (first commit wins).

A shard of a job attempt may be computed by several tasks (a retry after a
lost worker, or a speculative duplicate of a straggler). Exactly one of them
commits. The commit record, holding the winning task and its output, is one
durable key per (job, attempt, shard); it is checked and written with no
suspension point in between, so concurrent commit handlers cannot both win.
The reduce stage reads only these records.
"""

from . import ids


class CommitLog:
    """Committed shard outputs, cached in memory and backed by disk."""

    def __init__(self, disk):
        self.disk = disk
        self._cache = {}

    def _load(self, jkey, attempt):
        k = (jkey, int(attempt))
        got = self._cache.get(k)
        if got is None:
            got = {}
            for key in self.disk.keys(ids.commit_prefix(jkey, attempt)):
                rec = self.disk.get(key)
                got[int(rec["shard"])] = rec
            self._cache[k] = got
        return got

    def get(self, jkey, attempt, shard):
        return self._load(jkey, attempt).get(int(shard))

    def committed_shards(self, jkey, attempt):
        return sorted(self._load(jkey, attempt))

    def count(self, jkey, attempt):
        return len(self._load(jkey, attempt))

    def try_commit(self, jkey, attempt, shard, tid, worker, output):
        """Commit `output` for the shard unless another task already has.

        Returns (won, record, fresh): `won` is True when `tid` holds the
        commit, including a repeated commit by the task that already won;
        `fresh` is True only for the call that wrote the record.
        """
        shard = int(shard)
        known = self._load(jkey, attempt)
        rec = known.get(shard)
        if rec is not None:
            return rec["task"] == tid, rec, False
        rec = {"job": jkey, "attempt": int(attempt), "shard": shard, "task": tid,
               "worker": worker, "output": output}
        self.disk.put(ids.commit_key(jkey, attempt, shard), rec)
        known[shard] = rec
        return True, rec, True

    def inputs(self, jkey, attempt):
        """Reduce inputs: every committed output of the attempt, in shard order."""
        known = self._load(jkey, attempt)
        return [{"shard": s, "attempt": int(attempt), "task": known[s]["task"],
                 "output": known[s]["output"]} for s in sorted(known)]

    def forget(self, jkey):
        for k in [k for k in self._cache if k[0] == jkey]:
            del self._cache[k]
