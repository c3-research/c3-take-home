# shardmr changelog

Status: CURRENT. Newest first.

## 2.2

- Losing speculative tasks are aborted through the `abort` list of their
  worker's next heartbeat reply instead of a separate `abort` message. A loser
  that finishes first is turned away by the commit log as before.
- `worker_dead_after` now includes the pause allowance: it is
  `WORKER_TIMEOUT_S * (1 + MAX_DRIFT) + PAUSE_ALLOWANCE_S` = 5.0006 s (was
  `WORKER_TIMEOUT_S * (1 + MAX_DRIFT)` = 3.0006 s in 2.1). Healthy workers that
  paused for up to 2.0 s were being evicted.

## 2.1

- Worker restarts: the new incarnation resolves and releases every operation
  its predecessor recorded on disk before it registers. An operation that
  reports `NOT_FOUND` is checked again after `RECOVERY_SETTLE_S = 1.0` s.
- Releases wait for `ReleaseComplete` (`status.released` or the
  `release_complete` message) before a task reports, following
  `platform/gpu-api-v3.md` section 3.4.
- Ambiguous submits (timeout or `UNAVAILABLE`) are resolved with `status` and
  resubmitted with the same `op_id`, following `platform/gpu-api-v3.md`
  section 4. The 1.x behaviour of retrying under a new `op_id` ran some maps
  twice.

## 2.0

- Job attempts. A shard that fails `MAX_TASK_FAILURES = 2` times abandons the
  attempt; a new attempt maps every shard again; at most
  `MAX_JOB_ATTEMPTS = 3` attempts.
- Commit log keyed by `(job, attempt, shard)`, **first commit wins**, replacing
  the 1.x last-writer-wins store keyed by `(job, shard)`. The reduce reads only
  the commit log of the attempt it reduces.
- Worker epochs and fencing (`platform/leases-and-fencing.md`). Declaring a
  worker dead advances its epoch.
- Jobs are scoped by `(tenant, job_id)` instead of `job_id`.
- Batched assignment (`MAX_BATCH_TASKS = 3`) with per-task outcomes, so a
  partially failed batch re-runs only its failed shards.
- Speculation limited to `MAX_SPECULATIVE_PER_SHARD = 1` duplicate, launched
  after `duration + STRAGGLER_SLACK_S` (2.0 s) once half the shards have
  committed (1.x: 2 duplicates after 1.5 x duration).

## 1.0

- Initial release. See `design-v1.md` (superseded).
