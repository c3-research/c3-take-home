# Runbook: the commit protocol, grants and late duplicates

Status: CURRENT (service version 2.2). Complements `design-overview.md`
section 4 and `speculative-execution.md`.

## 1. The rule

The commit protocol has one rule: **the first committed attempt wins; later
duplicates must abort.** Commits are first-writer-wins per
`(job, attempt, shard)`. The first commit `coord` accepts for a shard writes
the shard's commit record, and that record is never replaced. Every other task
of the same shard is a later duplicate, however it got there (a speculative
copy, the straggler it copied, or a retry after a lost worker). A later
duplicate is answered `{committed: false, winner: <task>}`, reports
`duplicate` and releases its GPU operation. It never counts as committed.

`design-v1.md` (1.x, superseded) kept the *latest* output of each shard and
reasoned that order did not matter because every task of a shard computes the
same output. Neither half of that carries over to 2.x.

## 2. A grant belongs to one task, not to an output

`committed: true` in a commit reply is a **grant**. `coord` returns it only to
the task named in the shard's commit record: the first time that task commits,
and again if it repeats its own commit because a reply was lost. Two replies
with `committed: true` naming different tasks of one shard are a double commit
(invariant B1), and on-call treats them as a correctness incident.

Map outputs are deterministic, so a late duplicate's output is always
byte-for-byte equal to the committed output. That does **not** make it the
winner. Output equality is never a reason to grant a commit. `coord` compares
the task id with the record, never the payload.

## 3. The rule holds in every job state

The rule is not limited to the MAPPING state. When the last shard of an
attempt commits, the job moves to REDUCING, and every task of the attempt
that is still live is revoked with reason `map_complete`. On `coord` this
shows up as `task_finished ... state=revoked outcome=map_complete`. The
scheduler forgets those tasks at that point.

A revoked duplicate may already be past its GPU operation, so it still offers
its commit, often a few hundred milliseconds after the winner. `coord` then
answers from the commit record alone, in REDUCING and also after the job has
reached SUCCEEDED or FAILED:

| Commit from | Reply | Logged |
| --- | --- | --- |
| the task named in the record (a repeated commit) | `{committed: true, winner: <same task>}` | nothing new |
| any other task of the shard | `{committed: false, winner: <record's task>}` | `commit_lost` on `coord`, `task_commit_lost` on the worker, outcome `duplicate` |

A job that ends SUCCEEDED with the right result can still have had a double
commit. The reduce reads the same bytes either way, so B3 passes while B1
fails. Check the grants, not the result.

## 4. Checking a suspected double commit

1. On `coord`, find the shard's `shard_committed` record (one per
   `(job, attempt, shard)`) and note its `task`.
2. On every worker, grep `event=task_committed` for tasks of that shard. There
   must be exactly one, and it must be the task from step 1. A second
   `task_committed` for a task that `coord` revoked with `map_complete` means
   a late duplicate was granted.
3. `python -m sim check` reports it as B1: `... committed by 2 tasks`.
