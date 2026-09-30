# Runbook: releases that span a router restart

Status: CURRENT (jobrouter 3.2). Complements `router-restart.md` and
`capacity-and-releases.md`.

## Which operations are being released

A job can own more than one GPU operation. Each attempt has its own op_id
(`<tenant>/<job_id>/a<n>`), and a new attempt starts as soon as the router
applies a `FAILED` report for the previous one. The previous attempt's
operation is then still being released (`rel = releasing` in the job record)
while the job is QUEUED again or already LEASED under the next attempt. So at
any moment the operations in `releasing` are:

- the final operation of jobs that just became SUCCEEDED, FAILED or CANCELLED,
  and
- the operation of an **earlier attempt** of jobs that are still running,
  whenever an attempt has just failed.

## Recovery must restart every one of them

On boot the router restarts one release task per operation marked `releasing`,
across all attempts of every job, not only the job's current attempt. The
`releasing` field of `router_start` counts operations, not jobs. The capacity
ledger is rebuilt from the same records, so an operation whose release task is
not restarted keeps its units reserved for the life of the deployment: nothing
else ever releases it or marks it freed.

Symptoms of a missed release after a restart: `router_stats` shows `reserved`
that never returns to the level implied by running jobs, `dispatch_blocked` for
the largest jobs that never clears, and those jobs stay QUEUED indefinitely.
Units lost this way cannot be recovered by hand (`router-restart.md`, "Do not").

## Checks after a restart

- For every op_id marked `releasing` before the crash (`report_applied` with no
  `capacity_freed` yet), expect `release_requested` and then `capacity_freed`
  after the `router_start` line, including op_ids whose `report_applied` had
  `op_state=FAILED` and `state=QUEUED`.
- `router_start ... releasing=N` should equal the number of such op_ids.
