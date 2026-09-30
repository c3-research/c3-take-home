# shardmr API reference

Status: CURRENT (service version 2.2).

All methods are platform RPCs to the node `coord` unless stated otherwise.
Payloads and replies are JSON objects; errors are `RpcError(code, message)`.

## Client API

### `submit_job`

Request: `{tenant: str, job_id: str, shards: int, records: int, units?: int, duration?: float}`

| Field | Meaning | Limits |
| --- | --- | --- |
| `shards` | Number of map shards | 1 to 64 (`MAX_SHARDS_PER_JOB`) |
| `records` | Input records per shard | 1 to 4096 (`MAX_RECORDS_PER_SHARD`) |
| `units` | GPU units per map operation | 1 to 4 (`MAX_UNITS_PER_SHARD`), default 1 |
| `duration` | Requested map operation duration, seconds | > 0, default 1.0 (`DEFAULT_MAP_DURATION_S`) |

Reply: `{accepted: true, state, attempt}`.

- **Idempotent per `(tenant, job_id)`.** The scope is the pair: two tenants may
  use the same `job_id`. A repeated submit with the same `shards`, `records`,
  `units` and `duration` returns the job's current state and starts nothing.
  Job records never expire, so the key lifetime is the lifetime of the
  deployment.
- `RpcError("CONFLICT")`: a job with this `(tenant, job_id)` exists with
  different parameters.
- `RpcError("INVALID")`: a field is missing or out of range.
- If the job was cancelled before it was first submitted (see `cancel_job`),
  the submit is accepted and the reply's `state` is `CANCELLED`.

### `job_status`

Request: `{tenant, job_id}`.
Reply: `{state, attempt, shards?, committed?, result?}`.

- `state` is one of `PENDING`, `MAPPING`, `REDUCING`, `SUCCEEDED`, `FAILED`,
  `CANCELLED`. The last three are terminal and never change.
- `committed` is the number of shards committed in the current attempt.
- `result` (only when SUCCEEDED) is `{shards, count, sum, checksum}`.
- `RpcError("NOT_FOUND")` if no such job has been submitted or cancelled.

### `cancel_job`

Request: `{tenant, job_id}`. Reply: `{state}`.

- A non-terminal job becomes CANCELLED; its live tasks are revoked and any GPU
  operation it has open is cancelled and released.
- On a terminal job the call changes nothing and returns the terminal state.
- On an unknown job the cancel is **recorded** (a tombstone), so a submit that
  arrives later resolves as if the cancel had followed it.

## Worker protocol

### `register` (worker -> coord)

Request: `{worker, boot, reg_seq}`. Reply: `{epoch, gen}`.

`(boot, reg_seq)` orders a worker's registrations: `boot` is the worker's boot
count and `reg_seq` counts registrations within one boot. A registration older
than the newest one seen is rejected with `STALE_REGISTER`. A repeat of the
newest registration returns the same epoch. Any newer registration gets a new,
strictly larger epoch (persisted before the reply) and revokes every task the
worker held.

### `heartbeat` (worker -> coord)

Request: `{worker, epoch, hb_seq, running: [task_id]}`.
Reply: `{ok: true, abort: [task_id]}` or `{reregister: true}` if `epoch` is not
the worker's current epoch. Heartbeats older than one already processed
(`hb_seq` not increasing) are acknowledged but not reconciled.

### `assign` (coord -> worker)

Request: `{worker, epoch, batch, tasks: [descriptor]}`.
Reply: `{accepted: [task_id], hb_seq}`.

A descriptor is `{task, job, tenant, attempt, shard, records, units, duration,
speculative}`. The worker rejects an assignment whose `epoch` is not its current
epoch with `STALE_EPOCH`. A repeated `batch` id returns the original reply. A
task the worker was already told to abort is not accepted.
`coord` sends each assignment at most `ASSIGN_MAX_ATTEMPTS = 3` times with a
0.5 s timeout (`ASSIGN_RPC_TIMEOUT_S`); if none is answered it sends `abort`
for the batch and queues its shards again.

### `abort` (coord -> worker, one-way)

Payload `{tasks: [task_id]}`. The worker cancels those tasks' GPU operations.

### `commit` (worker -> coord)

Request: `{worker, epoch, task, job, attempt, shard, output}`.
Reply: `{committed: bool, winner: task_id|null}`.

| Error | Meaning | Task outcome reported |
| --- | --- | --- |
| `FENCED` | `epoch` is not the worker's current epoch | `fenced` |
| `STALE_ATTEMPT` | `attempt` is not the job's current attempt | `stale` |
| `UNKNOWN_TASK` | `coord` did not dispatch this task to this worker | `fenced` |
| `REVOKED` | the task was revoked | `fenced` |
| `INVALID` | `output` does not belong to `shard` | `fenced` |

The worker tries a commit at most `COMMIT_MAX_ATTEMPTS = 5` times, with a 0.5 s
timeout (`COMMIT_RPC_TIMEOUT_S`) and a backoff of 0.2 s x attempt number
(`COMMIT_BACKOFF_S`) before each retry. Retries carry the same task id, so a
retried commit that already won is answered `committed: true`.

### `report` (worker -> coord)

Request: `{worker, epoch, batch, results: [{task, outcome}]}`. Reply `{ok: true}`.
`FENCED` if the epoch is not current. Outcomes: `committed`, `duplicate`,
`failed`, `aborted`, `fenced`, `stale`, `commit_unknown`. Only `failed` counts
against the shard's `MAX_TASK_FAILURES = 2`; every outcome other than
`committed` queues the shard again if it is still uncommitted. The worker tries a
report at most `REPORT_MAX_ATTEMPTS = 4` times, with a 0.5 s timeout and a
backoff of 0.2 s x attempt number.

## GPU usage (for reference)

| Operation | `op_id` | Units | Duration |
| --- | --- | --- | --- |
| Map | `map/<task_id>` | job's `units` | job's `duration` |
| Reduce | `reduce/<tenant>:<job_id>/a<attempt>/r<try>` | 1 (`REDUCE_UNITS`) | 0.5 s (`REDUCE_DURATION_S`) |

GPU RPCs use a 1.0 s timeout (`GPU_RPC_TIMEOUT_S`).
