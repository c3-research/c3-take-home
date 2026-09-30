# Runbook: batch reports that never arrive

Status: CURRENT (service version 2.2). Complements the `report` entry in
`api-reference.md` and `design-overview.md` sections 3 and 6.

## 1. What a worker does with a report it cannot deliver

A worker sends a batch's report at most `REPORT_MAX_ATTEMPTS` times. When the
last attempt times out it logs `batch_report_abandoned` and **forgets the
batch's tasks anyway**, exactly as after a delivered report: they leave its
task table and drop out of its next heartbeat. The outcomes are not kept for a
later report and are never sent again.

Nothing is lost by this. `coord` still counts those tasks as running, sees
them missing from a heartbeat sent after the worker accepted them, marks them
lost (`missing_from_heartbeat`) and re-queues every shard that has not
committed. A worker must never keep a finished task in its running list:
`coord` treats a listed task as alive, so a task that stays listed after its
batch is over is never re-queued and its shard never finishes. A failed task
in such a batch hangs its job.

## 2. Diagnosis

| What you see | Meaning |
| --- | --- |
| `batch_report_abandoned` on a worker, then heartbeats that keep listing the batch's tasks | The batch is pinned; its uncommitted shards will never be re-queued |
| A job in MAPPING whose missing shard's task shows `task_done outcome=failed` on the worker but never `task_finished` on `coord` | The failure was never reported and nothing replaced the task |

Report timeouts are expected during a coordinator/worker cut of a few seconds
that is shorter than the dead-after budget (`operational-notes.md` section 2);
longer cuts fence the worker instead.
