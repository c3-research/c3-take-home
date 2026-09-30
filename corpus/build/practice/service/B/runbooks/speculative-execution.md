# Runbook: stragglers and speculative execution

Status: CURRENT (service version 2.2).

## 1. When a duplicate is launched

In every dispatch tick, for each job whose current attempt has at least half of
its shards (rounded up) committed (`SPECULATION_MIN_COMMITTED_FRACTION = 0.5`):
any shard with exactly one live task that has been running for longer than
`duration + STRAGGLER_SLACK_S` (`STRAGGLER_SLACK_S = 2.0` s) on the
coordinator's clock since dispatch gets one speculative task
(`MAX_SPECULATIVE_PER_SHARD = 1` per shard per attempt) on the least-loaded
other worker with a free slot. `speculation_launch` logs the straggler, its
elapsed time and the threshold.

## 2. Who wins

The original and the duplicate run as two GPU operations with two `op_id`s
(they are different tasks) and both may finish. Each offers its output to the
commit log. The first commit accepted for `(job, attempt, shard)` wins and is
the only output the reduce ever reads. The other is answered
`{committed: false, winner: <task>}` (`commit_lost` on `coord`,
`task_commit_lost` on the worker) and reports `duplicate`. Both map outputs are
identical, because a shard's output depends only on the job and the shard.

After a commit, the losing tasks are listed in the `abort` field of their
worker's next heartbeat reply (`task_abort_request reason=shard_committed`).
A loser that is still on the GPU is cancelled; one that already finished is
released as usual.

## 3. Things that look wrong but are not

- Two `gpu_effect finished` records for one shard: expected with speculation.
- `commit_lost` shortly after `shard_committed` for the same shard: the
  duplicate finished before the abort reached it.
- A speculative task winning: the original was a straggler.

## 4. Things that are wrong

- Two `shard_committed` records for one `(job, attempt, shard)`.
- A reduce whose inputs name a task other than the committed one, or a shard
  from another attempt.
Either breaks the invariants in `INVARIANTS.md` (B1, B2).
