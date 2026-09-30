# Runbook: jobs stuck or slow to finish

Status: CURRENT (jobrouter 3.2).

Symptoms: clients poll `status` for a long time; `router_stats` shows `queued`
growing; tenants report jobs sitting in QUEUED or LEASED.

## 1. Is the router dispatching?

Look at `router_stats` (every 10 s). Compare `reserved` with `capacity`.

- `reserved` at or near `capacity` with a growing queue: the GPU is full.
  Go to `capacity-and-releases.md` to check that finished operations are being
  released and freed.
- `reserved` well below `capacity` and a `dispatch_blocked` line: the oldest
  queued job needs more units than are free, and the router is holding
  capacity back for it (for at most `HOL_HOLD_S` = 10 s). This is expected
  while large jobs are queued behind small ones.
- `reserved` well below `capacity`, no `dispatch_blocked`, `grants` flat:
  workers are not acquiring. Check `worker_start`/`worker_registered` on each
  worker and `acquire_retry` / `acquire_rejected` lines.

## 2. A job is LEASED but nothing happens

Find the job's `lease_granted` line (router) and the matching `lease_acquired`
line (worker, same `key` and `epoch`).

- No `lease_acquired`: the grant reply was lost, or the worker judged the
  lease stale on arrival (`lease_stale_on_arrival`). The lease is not renewed,
  so the router returns the job to the queue after the re-grant wait of
  2.504 s (`lease_expired`) and grants it again with epoch + 1 and the same
  op_id.
- `lease_acquired` then `lease_lost` / `lease_abandoned`: the holder stopped
  acting (fenced, expired, or its lease budget of 1.748 s ran out while renewals
  were failing). Expected under network trouble; the next holder adopts the same
  GPU operation.
- Repeated `lease_expired` for the same key with increasing epochs: renewals are
  not getting through. Check for a partition between the router and the
  workers, or a router that pauses for longer than the lease.

A new holder never starts a second GPU operation for the same attempt: all
holders of an attempt share its op_id.

## 3. A job went through several attempts

`report_applied ... op_state=FAILED` increments the attempt and requeues the
job; each attempt has a new op_id (`.../a2`, `.../a3`, ...). After 8 failed
operations (`MAX_GPU_ATTEMPTS`) the job is FAILED. A job is never retried under
a new op_id for any other reason.

## 4. Do not

- Do not lengthen `LEASE_S` to "give workers more time": holders renew every
  0.5 s, and a longer lease only delays recovery from a dead or partitioned
  worker.
- Do not shorten the re-grant wait below `LEASE_S * (1 + 2 * DRIFT_RATE) +
  REGRANT_GRACE_S`: a holder with a slow clock could still believe it holds the
  lease.
