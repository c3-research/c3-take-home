# Runbook: a holder acted after its lease ran out

Status: CURRENT (jobrouter 3.2).

Use when the lease checks report a worker that submitted, cancelled or reported
under an epoch after the router's lease for that epoch had expired, when two
holders of the same job were both acting (a `report` under epoch `e` arriving
after the router granted `e + 1`), or when `lease_fenced` lines on the router
cluster around router pauses or partitions.

## 1. What a holder may do, and when

A worker's lease is valid on its own clock until the lease's send time plus
the holder budget (1.748 s, `operational-notes.md`, "Leases"). Validity is a
property of *time*, so it has to be read off the clock at the moment of each
action:

- **Every send is a protected action, retries included.** The holder calls
  `HolderLease.valid()` immediately before each GPU `submit` and `cancel` it
  sends and before each `report` it sends. A `report` retried after a timeout
  or a `job_report_retry` backoff is a new action and needs its own check.
  Having been valid when the first attempt went out proves nothing about the
  second: the usual reason a report times out is that the router is paused or
  cut off, which is exactly when renewals stop and the lease runs down.
- **`lease.lost` is not a validity check.** It records why the holder stopped
  (`expired`, `fenced`, `reported`, ...). The renew task sets `expired` only
  when it next wakes up, which can be up to `RENEW_INTERVAL_S` +
  `RENEW_TIMEOUT_S` (0.9 s) after the lease actually ran out, and longer if
  the worker pauses. A loop that runs "while the lease is not lost" keeps
  acting in that gap.
- **Fencing does not make a late action safe.** The router rejects a report
  under a stale epoch, but only after the job has been re-granted. A report
  that reaches it after the lease expired and before the re-grant (the
  2.504 s re-grant wait) is applied, and a GPU `submit` or `cancel` is not
  fenced by the router at all. The lease rules in
  `platform/leases-and-fencing.md` section 3 ("re-check immediately before
  acting", "stop acting at expiry") apply to every send.

When `valid()` fails, the holder raises `LeaseLost` and abandons the attempt.
The next holder adopts the same op_id, finds the operation's terminal state
with `status` and reports it.

## 2. Check the holder side

1. Take the violating action from the check output (key, epoch, time) and
   find the worker's `job_report` / `gpu_submit` / `gpu_cancel` line for it.
2. Look back for the last `lease_renewed` for that key and epoch on the router,
   and the worker's `lease_renew_timeout` lines after it. If the worker sent
   `job_report` lines after its budget from the last acknowledged renew had
   run out, and `lease_expired_local` or `lease_abandoned` only appears after
   them, the report loop is not re-checking the lease before each attempt.
3. A `job_report_retry reason=timeout` followed by another `job_report` more
   than 1.748 s after the last acknowledged renew's send is the signature.

## 3. Things that do not fix it

- Checking the lease once before the first report attempt. The first attempt
  follows a fresh check anyway; it is the retries that go out late.
- Lengthening the re-grant wait or `LEASE_S`. The holder must stop at its own
  expiry whatever the router does afterwards.
- Making the renew task wake more often. It narrows the gap but a paused or
  slow renew still leaves the report loop acting on an expired lease.
