# Runbook: cancellations and their outcomes

Status: CURRENT (jobrouter 3.2).

Use when a tenant disputes the outcome of a job it cancelled: the job shows
CANCELLED but platform accounting shows GPU time for it (an operation that
finished `SUCCEEDED`, or kept running after the cancel was acknowledged), or
the job shows SUCCEEDED although the tenant cancelled it.

## 1. Two ways to cancel

The router's `cancel` handler picks one of two paths (`design-overview.md`
section 7). The choice depends on whether a GPU operation can exist for the
job's current attempt, **not** on the job's current state:

| Current attempt | Path | Router log |
| --- | --- | --- |
| never granted | CANCELLED at once; nothing can be on the GPU for it | `job_terminal ... reason=cancel_queued` |
| granted at least once | reply `CANCELLING`; the holder cancels the operation and reports its terminal state | `cancel_requested`, later `report_applied` |

The immediate path is safe only because an attempt's op_id is first submitted
under a lease: before the first grant of the attempt no holder has ever seen
its op_id.

## 2. QUEUED does not mean "not started"

A job is QUEUED in three situations:

1. it was submitted and has not been granted yet;
2. its previous attempt's operation was reported `FAILED` and the next attempt
   has not been granted yet (a new op_id, never submitted);
3. its lease ran out (`lease_expired`) or was revoked (`lease_revoked`) and the
   job is waiting to be granted again **for the same attempt**.

In case 3 the attempt's operation was very likely submitted by the previous
holder, may still be running, and may already have finished `SUCCEEDED`; the
previous holder simply stopped watching it. The job record's `granted` field
records "the current attempt has been granted at least once": it is set by the
first grant of an attempt and cleared only when a new attempt starts (after a
`FAILED` report). Returning a job to the queue on lease expiry or revocation
must leave it set, so that a cancel for that job takes the `CANCELLING` path:
the next holder, granted in `mode=cancel`, adopts the same op_id, cancels it if
it is still running, and reports the real outcome. A completed operation wins
over a pending cancel, so such a job ends SUCCEEDED with its result.

Cancelling a case-3 job at once records CANCELLED while its operation runs on:
the tenant is billed for GPU work the service says was cancelled, the
operation's units are never released, and platform accounting shows a
`SUCCEEDED` operation for a CANCELLED job.

## 3. Checks

- For a disputed job, look for `lease_expired` or `lease_revoked` for the job
  on the router, followed by `job_terminal ... reason=cancel_queued`. That
  sequence is a service fault: after a grant the cancel must be
  `cancel_requested` and end with `report_applied`.
- `gpu_cancel_sent` followed by `gpu_op_state ... state=CANCELLED` on a worker
  means the cancel took effect before the operation finished.
