# shardmr design overview

Status: CURRENT (service version 2.2). Supersedes `design-v1.md`.

shardmr runs sharded map-reduce jobs on the C3 GPU service. A client submits a
job with a number of shards; every shard is mapped by one GPU operation, and
once every shard has a committed output a single reduce operation combines
them into the job's result. The service assumes exactly what
`platform/runtime-v3.md` guarantees and nothing stronger, and follows
`platform/gpu-api-v3.md`, `platform/idempotency.md` and
`platform/leases-and-fencing.md`.

## 1. Nodes

| Node | Role |
| --- | --- |
| `coord` | The single coordinator. Owns every job record, the commit log, worker membership, the scheduler and the reduce stage. All client and worker RPCs go to it. |
| `worker-1` ... `worker-N` | Map workers. Each registers with `coord`, heartbeats, runs the map tasks it is assigned as GPU operations, offers their outputs to the commit log and reports each batch. |
| `gpu` | The platform GPU service. shardmr is deployed against **12 units** of capacity. |

## 2. Jobs and attempts

A job is identified by `(tenant, job_id)` and moves through

```
PENDING -> MAPPING -> REDUCING -> SUCCEEDED
              ^   \        |
              |    `-------+---> FAILED / CANCELLED
              `--(attempt n+1)
```

Every job record is one disk key on `coord`, so each state transition is one
atomic write. A job runs in **attempts**, numbered from 1. An attempt maps every
shard of the job. If a shard of the current attempt fails on the GPU
`MAX_TASK_FAILURES = 2` times, the attempt is **abandoned** and a new attempt
maps every shard again; nothing committed by an abandoned attempt is ever read.
A job gets at most `MAX_JOB_ATTEMPTS = 3` attempts before it is marked FAILED.

## 3. Tasks, batches and slots

A *task* is one try at mapping one shard of one attempt. Its id is
`<tenant>:<job_id>/a<attempt>/s<shard>/g<gen>.<n>`, where `gen` is the
coordinator generation (a counter persisted on `coord` and advanced on every
coordinator boot) and `n` a per-generation counter. A task id is therefore never
reused, even across coordinator restarts. Each task runs as **exactly one** GPU
operation, `op_id = "map/" + task_id`.

Every dispatch tick (`DISPATCH_INTERVAL_S = 0.1` s) the scheduler hands each live
worker a **batch** of up to `MAX_BATCH_TASKS = 3` queued shards, bounded by the
worker's free slots (`WORKER_SLOTS = 3` tasks assigned and not yet reported per
worker). The worker runs a batch's tasks concurrently and reports the batch once
all of them have finished, with one outcome per task. A batch can therefore
**partially fail**: each task's outcome is applied on its own, and only the
shards whose task failed are queued again. Shards whose task committed are
never re-run.

## 4. The commit log (first commit wins)

A shard of an attempt may be computed by more than one task: a retry after a
lost worker, or a speculative duplicate. Exactly one of them commits. The commit
log holds one durable record per `(job, attempt, shard)`, written by the first
accepted commit and never overwritten. The check for an existing record and the
write happen with no suspension point between them, so two commit handlers
running concurrently cannot both win. A later commit for the same shard is
answered `{committed: false, winner: <task>}`; a repeat of the winning task's own
commit is answered `{committed: true}` without writing anything.

A commit is accepted only if **all** of these hold (see `api-reference.md`):

1. the sender's worker epoch is its current epoch (fencing, section 6);
2. the commit names the job's current attempt;
3. the task is one the scheduler dispatched to that worker for that job,
   attempt and shard, and has not been revoked;
4. the job is MAPPING.

## 5. Speculative execution

Once at least `SPECULATION_MIN_COMMITTED_FRACTION = 0.5` of a job's shards
(rounded up) have committed in the current attempt, any task that has been
running longer than its shard's `duration + STRAGGLER_SLACK_S` (`2.0` s),
measured on the coordinator's clock from dispatch, is a **straggler**. It gets
at most `MAX_SPECULATIVE_PER_SHARD = 1` speculative duplicate per attempt, on a
different worker with a free slot. The duplicates race to commit; the commit
log keeps the first. The losing tasks are told to abort in their worker's next
heartbeat reply. A loser that finishes before that offers its output and is
turned away by the commit log.

## 6. Workers, epochs and fencing

Each registration of a worker incarnation is issued a strictly larger
**worker epoch**, persisted on `coord` before it is returned. Assignments,
commits and reports carry the epoch they were issued under. A worker rejects an
assignment whose epoch is not its current one, and `coord` rejects commits and
reports whose epoch is not the worker's current one (`FENCED`).

`coord` declares a worker dead when it has heard no heartbeat from it for
`worker_dead_after = WORKER_TIMEOUT_S * (1 + MAX_DRIFT) + PAUSE_ALLOWANCE_S`
= 3.0 x 1.0002 + 2.0 = **5.0006 s** on its own clock. Declaring a worker dead
advances its epoch, so a worker that was only paused is fenced when it resumes:
its next heartbeat is answered `{reregister: true}`, it aborts everything it
holds and registers again. Every task the dead or re-registered worker held is
revoked and its shard queued again.

Heartbeats (`HEARTBEAT_INTERVAL_S = 0.5` s) list the tasks the worker holds.
`coord` reconciles the list: a task the worker holds that `coord` no longer
considers live is returned in `abort`; a task `coord` considers running that is
missing from a heartbeat sent after the worker accepted it is marked lost and
its shard queued again. The assignment reply carries the worker's heartbeat
sequence number at acceptance, which is how `coord` tells "sent after".

## 7. GPU usage

Workers and the reduce stage use the GPU through one client component that
applies the platform rules:

- `op_id`s are derived from persisted identifiers and written to the caller's
  disk **before** the first submit;
- a submit that times out or returns `UNAVAILABLE` is an unknown outcome and is
  resolved with `status` before anything else; `NOT_FOUND` means resubmit with
  the **same** `op_id`;
- `CAPACITY` is retried with exponential backoff from 0.25 s to 2.0 s;
- completion is learned by polling `status` every 0.25 s;
- every accepted operation is released, and the caller waits for
  `ReleaseComplete` (`status.released` or the `release_complete` message) before
  it treats the units as free or reports the task.

## 8. Reduce

When every shard of the current attempt has committed, the job moves to
REDUCING and the coordinator runs one reduce operation (1 unit, 0.5 s) whose
payload is built **only** from the commit log of that attempt, one input per
shard. Its `op_id` is `reduce/<tenant>:<job_id>/a<attempt>/r<try>` and is added
to the job's `open_ops` on disk before the submit, so a restarted coordinator
resumes the same operation. A reduce that ends FAILED is tried again under the
next `try` number, up to `MAX_REDUCE_FAILURES = 3` failures, after which the job
is FAILED.

## 9. Crashes

- **Coordinator restart:** job records, the commit log and worker epochs are on
  disk; tasks and membership are in memory and are rebuilt. MAPPING jobs are
  rescheduled (committed shards are kept), REDUCING jobs resume their reduce,
  terminal jobs release any operation still in `open_ops`. Workers find out on
  their next heartbeat (their epoch is no longer current) and re-register.
- **Worker restart:** the new incarnation first resolves every operation its
  predecessor recorded on disk (status; if it exists, cancel it, wait for a
  terminal state and release it) and then registers afresh. Nothing from before
  the restart is resumed.

See `operational-notes.md` for the full list of constants and deployment
assumptions.
