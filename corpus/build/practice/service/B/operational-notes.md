# shardmr operational notes and deployment assumptions

Status: CURRENT (service version 2.2). This is the authoritative list of the
numbers shardmr is built around. Every constant lives in
`src/shardmr/config.py` under the name given here.

## 1. Deployment assumptions

| Assumption | Value | Where it is used |
| --- | --- | --- |
| GPU capacity | 12 units | Workload sizing; shardmr does not query it |
| Clock drift between any two nodes | at most 200 ppm (`MAX_DRIFT = 200e-6`) | Failure detection |
| Clock offset between any two nodes | at most 0.5 s | Not budgeted: shardmr never compares timestamps taken on different nodes. Every interval (heartbeat silence, straggler age) is measured on `coord`'s own clock |
| Longest process pause of a healthy node | 2.0 s (`PAUSE_ALLOWANCE_S`) | Failure detection |
| Coordinator | exactly one node, `coord` (`COORDINATOR`) | |

## 2. Failure detection

| Constant | Value |
| --- | --- |
| `HEARTBEAT_INTERVAL_S` | 0.5 s between a worker's heartbeats |
| `HEARTBEAT_RPC_TIMEOUT_S` | 0.4 s |
| `WORKER_TIMEOUT_S` | 3.0 s |
| `LIVENESS_CHECK_INTERVAL_S` | 0.25 s between `coord`'s liveness sweeps |
| `REGISTER_RPC_TIMEOUT_S` | 0.5 s |
| `REGISTER_RETRY_S` | 0.5 s between registration attempts |

A worker is declared dead after

```
worker_dead_after = WORKER_TIMEOUT_S * (1 + MAX_DRIFT) + PAUSE_ALLOWANCE_S
                  = 3.0 * 1.0002 + 2.0 = 5.0006 s
```

of heartbeat silence, measured on `coord`'s clock from the last heartbeat it
processed. The drift term covers a worker whose clock runs slow (it sends
heartbeats less often than every 0.5 s of true time); the pause term covers a
worker frozen for up to 2.0 s. A healthy worker is never declared dead under
these assumptions. Declaring a worker dead advances its epoch (the fencing
rule of `platform/leases-and-fencing.md` section 6).

## 3. Scheduling

| Constant | Value |
| --- | --- |
| `WORKER_SLOTS` | 3 tasks per worker (assigned and not yet reported) |
| `MAX_BATCH_TASKS` | 3 tasks per `assign` batch |
| `DISPATCH_INTERVAL_S` | 0.1 s between dispatch ticks |
| `ASSIGN_RPC_TIMEOUT_S` | 0.5 s |
| `ASSIGN_MAX_ATTEMPTS` | 3 |
| `STRAGGLER_SLACK_S` | 2.0 s: a task is a straggler once it has run longer than `duration + 2.0` s since dispatch (coordinator clock) |
| `SPECULATION_MIN_COMMITTED_FRACTION` | 0.5: speculation starts once half the shards (rounded up) of the attempt have committed |
| `MAX_SPECULATIVE_PER_SHARD` | 1 duplicate per shard per attempt |
| `MAX_TASK_FAILURES` | 2 GPU failures of one shard in one attempt abandon the attempt |
| `MAX_JOB_ATTEMPTS` | 3 attempts per job, then FAILED |
| `MAX_REDUCE_FAILURES` | 3 failed reduce operations per attempt, then FAILED |

## 4. GPU client

| Constant | Value |
| --- | --- |
| `GPU_RPC_TIMEOUT_S` | 1.0 s for every GPU RPC |
| `GPU_POLL_INTERVAL_S` | 0.25 s between `status` polls |
| `CAPACITY_BACKOFF_INITIAL_S` / `CAPACITY_BACKOFF_MAX_S` | 0.25 s doubling to at most 2.0 s after `CAPACITY` |
| `UNKNOWN_OUTCOME_BACKOFF_S` | 0.1 s before re-probing after a timeout or before resubmitting after `NOT_FOUND` |
| `RELEASE_POLL_INTERVAL_S` | 0.5 s between `released` checks while waiting for `ReleaseComplete` |
| `RECOVERY_SETTLE_S` | 1.0 s: after a worker restart, an operation its predecessor recorded that reports `NOT_FOUND` is checked once more after this delay before the record is dropped |
| `REDUCE_UNITS` | 1 unit |
| `REDUCE_DURATION_S` | 0.5 s |

An ambiguous submit (timeout or `UNAVAILABLE`) is always followed by `status`
with the same `op_id`; a new `op_id` is never allocated for a retry
(`platform/gpu-api-v3.md` section 4).

## 5. Worker to coordinator

| Constant | Value |
| --- | --- |
| `COMMIT_RPC_TIMEOUT_S` | 0.5 s |
| `COMMIT_MAX_ATTEMPTS` | 5 |
| `COMMIT_BACKOFF_S` | 0.2 s x attempt number before each retry |
| `REPORT_RPC_TIMEOUT_S` | 0.5 s |
| `REPORT_MAX_ATTEMPTS` | 4 |
| `REPORT_BACKOFF_S` | 0.2 s x attempt number before each retry |

## 6. Job limits

| Constant | Value |
| --- | --- |
| `MAX_SHARDS_PER_JOB` | 64 |
| `MAX_RECORDS_PER_SHARD` | 4096 |
| `MAX_UNITS_PER_SHARD` | 4 |
| `DEFAULT_MAP_DURATION_S` | 1.0 s |

## 7. Idempotency and identifiers

| Identifier | Scope | Lifetime |
| --- | --- | --- |
| Job key | `(tenant, job_id)` | Never expires |
| Worker epoch | per worker name, strictly increasing, persisted on `coord` before use | Never reused |
| Coordinator generation | persisted counter, advanced on every `coord` boot | Never reused |
| Task id | contains job, attempt, shard, generation and a counter | Never reused |
| Map `op_id` | `map/<task_id>`, recorded on the worker's disk before submit | GPU `op_id`s never expire |
| Reduce `op_id` | `reduce/<job>/a<attempt>/r<try>`, recorded in the job's `open_ops` before submit | |
| Commit record | one per `(job, attempt, shard)`, first writer wins | Kept for the job's lifetime |

## 8. Log events worth knowing

`coord`: `job_state`, `attempt_start`, `attempt_abandoned`, `task_dispatch`,
`batch_assigned`, `batch_report`, `shard_committed`, `commit_lost`,
`commit_rejected`, `shard_requeued`, `shard_exhausted`, `speculation_launch`,
`task_abort_request`, `worker_registered`, `worker_dead`, `reduce_submit`,
`job_succeeded`, `coord_recovered`.
Workers: `worker_registered`, `worker_fenced`, `batch_received`, `task_start`,
`task_committed`, `task_commit_lost`, `task_done`, `batch_reported`,
`recovery_op`, and the GPU client's `gpu_submit*`, `gpu_op_terminal`,
`gpu_release`, `gpu_released`.

## 9. How heartbeats are judged

A heartbeat request carries `worker`, `epoch`, `hb_seq` and `running`, and
nothing else. coord judges a heartbeat only by when it arrives, on its own
clock. It never compares a timestamp taken on a worker with its own clock,
because the clock offset between nodes (section 1) is not budgeted anywhere in
shardmr. Heartbeats from one worker are ordered by `hb_seq` alone. A heartbeat
that is not older than one already processed always refreshes the worker's
silence timer, however late it arrives: a late heartbeat still shows that the
worker was alive when it sent it.

The dead-after budget of section 2 is the only timing rule applied to
heartbeats. Its drift term is small because it scales an interval measured on
a single clock. It is not an allowance for comparing two clocks. When a worker
that never crashed is repeatedly declared dead (`worker_dead
reason=heartbeat_timeout`), check that each of its heartbeats reached the
silence timer before you change `WORKER_TIMEOUT_S` or `PAUSE_ALLOWANCE_S`.
