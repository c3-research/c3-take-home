# Runbook: a job is not finishing

Status: CURRENT (service version 2.2).

Symptom: a client keeps polling `job_status` and the job stays in MAPPING or
REDUCING. Timestamps in logs are node-local (`platform/runtime-v3.md`
section 8), so compare events on one node at a time.

## 1. Find the job's attempt and progress

On `coord`, grep `event=job_state job=<tenant>:<job_id>` and
`event=attempt_start`. `job_status.committed` gives the number of shards
committed in the current attempt.

## 2. MAPPING with shards missing

For each missing shard, follow its tasks: `event=task_dispatch ... shard=<n>`
gives the task id and worker, then look for that task's `task_finished`
(`coord`) and `task_start` / `task_done` (worker).

| What you see | Meaning |
| --- | --- |
| `batch_assign_retry` then `batch_assign_failed` | The worker never answered 3 `assign` attempts; the shard is queued again |
| `shard_requeued` with rising `failures` | GPU operations of the shard end FAILED. At `MAX_TASK_FAILURES = 2` the attempt is abandoned (`shard_exhausted`, `attempt_abandoned`) and a new attempt starts, up to `MAX_JOB_ATTEMPTS = 3` |
| `gpu_submit_retry code=CAPACITY` on workers | The GPU is full. Units are only free after `ReleaseComplete`; check that workers log `gpu_released` for their finished operations |
| `gpu_submit_unknown` followed by `gpu_submit_confirmed` | Expected: a timeout or `UNAVAILABLE` resolved by `status` |
| `worker_dead` | The worker was silent for 5.0006 s (`operational-notes.md` section 2); its tasks are revoked and re-queued |

A shard with a live task and no progress for longer than `duration + 2.0` s
should get a speculative duplicate once half the job's shards have committed
(`speculation_launch`). See `speculative-execution.md`.

## 3. REDUCING

Look for `reduce_submit` and the reduce `op_id` (`reduce/<job>/a<attempt>/r<try>`).
A reduce that ends FAILED is logged `reduce_failed` with its try count; after
`MAX_REDUCE_FAILURES = 3` the job is FAILED. `reduce_incomplete` means the
commit log of the attempt did not hold every shard; the job goes back to MAPPING
for the missing shards.

## 4. After a coordinator restart

`coord_start gen=<g>` and `coord_recovered` list the jobs found on disk by
state. MAPPING jobs are rescheduled with their committed shards kept; tasks
from the previous generation are unknown to the new coordinator, so their
commits are rejected (`commit_rejected reason=unknown_task`) and the shards
are dispatched again. This is expected.

## 5. Do not

- Do not resubmit a job under a new `job_id` to "unstick" it: that runs every
  shard again and charges the tenant twice for the GPU time.
- Do not delete commit records from `coord`'s disk. The reduce reads only the
  commit log; a missing record sends the job back to MAPPING.
