# jobrouter: design overview

Status: CURRENT (jobrouter 3.2). Supersedes `design-overview-v2.md`.

jobrouter accepts GPU jobs from tenants, leases each job to a worker, and has
the worker run the job's work as an operation on the platform GPU service
(`platform/gpu-api-v3.md`). It is built on the C3 runtime (`platform/runtime-v3.md`)
and follows the platform rules for leases (`platform/leases-and-fencing.md`) and
idempotency (`platform/idempotency.md`). Every number quoted here is listed in
one place in `operational-notes.md`.

## 1. Nodes

| Node | Role | Durable state |
| --- | --- | --- |
| `router-1` | Owns the job table, grants and fences leases, accounts GPU capacity, releases finished GPU operations | One disk record per job (`job:<tenant>/<job_id>`) |
| `worker-N` | Leases jobs from the router and runs them on the GPU, `worker_slots` (2) at a time | None |
| `gpu` | Platform GPU service | Platform-managed |

There is one router. Workers are interchangeable; any number can be deployed.

## 2. Jobs

A job is identified by its idempotency key `(tenant, job_id)`. The key is
tenant-scoped: two tenants may use the same `job_id`. Job records are never
deleted, so the deduplication record for a key lives for the lifetime of the
deployment.

Job states:

```
 submit          grant             report SUCCEEDED
--------> QUEUED -------> LEASED --------------------> SUCCEEDED
            ^  ^            |  \   report CANCELLED, or
            |  |  lease ran |   \  any op end after a cancel request
            |  |  out /     |    `-----------------------> CANCELLED
            |  |  revoked   |
            |  `------------'      report FAILED, attempt < 8 -> QUEUED (next attempt)
            |                      report FAILED, attempt = 8 -> FAILED
            `-- cancel while QUEUED, current attempt never granted -> CANCELLED
```

SUCCEEDED, FAILED and CANCELLED are terminal and never change.

### Attempts and op_ids

Each job runs as one or more *attempts*. Attempt `n` of job `(tenant, job_id)`
uses the GPU operation id

    op_id = "<tenant>/<job_id>/a<n>"

The op_id is a pure function of the job key and the attempt number, so every
holder of every lease on the same attempt submits the same op_id, and the GPU's
op_id idempotency deduplicates them. A new attempt (and so a new op_id) is only
started after the router has recorded that the previous attempt's operation
ended `FAILED`. A job is declared FAILED after `MAX_GPU_ATTEMPTS` = 8 failed
operations. An attempt whose operation might still run is never given a new
op_id.

## 3. Leases, epochs and fencing

The router is the grantor; a worker slot is the holder.

- **Grant.** The router grants a lease on the oldest dispatchable QUEUED job
  (see section 5) in reply to `acquire`. Before replying it increments the
  job's epoch and writes the job record (state LEASED, epoch, holder, holder's
  boot count) to disk. Epochs per job are strictly increasing and are never
  reused, including across router restarts, because they live in the job record.
- **Length.** A lease lasts `LEASE_S` = 2.0 s on the router's clock from the
  moment the router processes the acquire or renew.
- **Renewal.** The holder renews every `RENEW_INTERVAL_S` = 0.5 s. A renew is
  accepted only for the job's current epoch, holder and holder boot, and only
  while the lease is still live on the router's clock.
- **Holder-side validity.** The holder measures its lease from the local time
  at which it *sent* the acquire (or the acknowledged renew), never from the
  reply's arrival and never from the router's `expires_at` field, which is
  informational only. It treats the lease as expired at

      sent_at + LEASE_S * (1 - DRIFT_RATE) - MAX_DRIFT_S
      = sent_at + 2.0 * (1 - 0.001) - 0.25 = sent_at + 1.748 s

  on its own clock. `DRIFT_RATE` = 1000 ppm is the deployment's clock drift
  bound and `MAX_DRIFT_S` = 0.25 s is the holder's safety margin ("max_drift").
  The holder re-checks validity immediately before every protected action
  (GPU submit, GPU cancel, report), with no await between the check and the
  send, and stops acting as soon as it is no longer valid, even if a renew is
  in flight.
- **Re-grant.** When a lease is not renewed, the router returns the job to the
  queue only after

      acked_at + LEASE_S * (1 + 2 * DRIFT_RATE) + REGRANT_GRACE_S
      = acked_at + 2.004 + 0.5 = acked_at + 2.504 s

  on its own clock, where `acked_at` is when it last granted or renewed the
  lease. The next grant carries epoch + 1 and the **same op_id** (the attempt
  is unchanged), so the new holder adopts the old holder's operation instead of
  starting a second one.
- **Fencing.** `renew` and `report` carry `(key, epoch, boot)`. The router
  rejects them with `FENCED` unless they name the job's current epoch, the
  current holder and the holder's current boot count, and the job is LEASED.
  The check and the state change happen in one handler step, with no await in
  between. A fenced holder stops acting on the job and does not retry.
- **GPU work is fenced by the service.** The GPU does not know about epochs.
  A worker only submits while its lease is valid, and duplicate submissions
  across holders share one op_id, so the GPU runs the attempt at most once.

### Worker incarnations

Workers keep no durable state. A restarted worker holds nothing: it registers
its new incarnation with `hello {boot}` before acquiring. When the router sees
a worker's boot count increase (in `hello` or `acquire`), it revokes every
lease held by that worker's earlier incarnations and returns those jobs to the
queue immediately, without waiting out the lease: the old incarnation is gone
and can no longer act, and anything it sent that is still in the network is
either deduplicated by op_id (GPU submits) or fenced by boot count (reports).
An `acquire` from an older incarnation than the newest one seen gets
`STALE_BOOT`; `renew` and `report` from an older incarnation are `FENCED`.

### Router restarts

The router rebuilds everything from its job records on boot
(`runbooks/router-restart.md`). It cannot know how much of a LEASED job's lease
elapsed while it was down, so it re-arms every such lease at full length from
boot: the job is re-granted no earlier than `regrant_after` (2.504 s) after the
router comes back. The holder keeps its epoch; its renewals and report are
accepted as before.

## 4. Running a lease (worker)

1. **Submit.** Submit the attempt's op_id. `CAPACITY` means nothing was created:
   back off and submit the same op_id again. A timeout or `UNAVAILABLE` is an
   unknown outcome (gpu-api-v3 section 4): call `status(op_id)` first, continue
   from the reported state if the operation exists, and submit again (same
   op_id) only on `NOT_FOUND`.
2. **Poll.** Poll `status` every `GPU_POLL_S` = 0.3 s until the operation is
   terminal. If the lease is in cancel mode, or a renew reply says a cancel was
   requested, send `cancel(op_id)` once it can be acknowledged.
3. **Report.** Send `report {key, epoch, boot, op_id, state, result?}` to the
   router. Retry on timeout while the lease is valid. The router records the
   outcome durably before replying.

A worker never releases GPU operations. Release is the router's job, because
the router owns capacity (section 6).

## 5. Dispatch

QUEUED jobs are dispatched in submission order. A job whose current attempt
already holds a capacity reservation (a re-grant, or a job being cancelled
after its first grant) is always dispatchable. Any other job needs its units
to fit in free capacity. When the oldest job that needs fresh capacity does not
fit, its units are held back and younger jobs are dispatched only from the
capacity beyond them, for at most `HOL_HOLD_S` = 10 s; after that the router
falls back to first-fit so that one blocked job cannot stall the queue.

## 6. Capacity

The GPU has no capacity query, so the router keeps a ledger. The deployment
capacity is stated in `operational-notes.md` (12 units). Units are *reserved*
for an attempt's op_id when the attempt's first lease is granted, before any
submit can reach the GPU, and stay reserved until the router has observed
**ReleaseComplete** for that op_id. A job whose `units` exceed the deployment
capacity is rejected at submit with `INVALID`.

Release sequence (router):

1. A terminal report is recorded, and the op is marked `releasing`, in one
   disk write.
2. The router calls `release(op_id)` (retrying on timeout with backoff).
3. The `{ok: true}` reply only means the release was recorded. The router keeps
   the units reserved until it sees either the GPU's `release_complete` message
   or `status(op_id).released == true`, polled every `RELEASE_POLL_S` = 0.5 s
   as a fallback because the message may be lost.
4. Only then is the op marked `freed` on disk and its units returned to the
   ledger.

Because the GPU commits units for a sub-interval of the router's reservation,
the GPU's committed units can never exceed capacity.

## 7. Cancellation

`cancel {tenant, job_id}` is idempotent and always names the job it cancels.

- **QUEUED, current attempt never granted:** the job becomes CANCELLED at
  once. No GPU operation exists for the current attempt, and every earlier
  attempt's operation has already ended FAILED.
- **Unknown job:** the router records a CANCELLED tombstone. A later `submit`
  for that key is accepted (the submit is acknowledged) and the job stays
  CANCELLED, so a cancel that overtakes its submit resolves as if they had
  arrived in order.
- **Current attempt granted at least once:** the router records the cancel
  request and replies `CANCELLING`. The current (or next) holder learns of it
  from the lease mode or a renew reply and cancels the GPU operation. The job
  ends CANCELLED if the operation ended CANCELLED or FAILED, and SUCCEEDED if
  the operation finished first: `cancel` is a request, not a guarantee (gpu-api-v3 section 3.3). After
  the router records a job as CANCELLED, no operation of that job produces a
  further effect.

## 8. Client retries

Clients retry `submit` and `cancel` with the same `(tenant, job_id)`. A retried
submit with the same units and duration returns the job's current state; one
with different parameters is rejected with `MISMATCH`. Clients learn completion
by polling `status`.
