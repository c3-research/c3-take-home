> SUPERSEDED by design-overview.md. Kept for reference.

# jobrouter 2.x: design overview

Status: SUPERSEDED (jobrouter 2.0–2.4). Do not use for current deployments;
see `design-overview.md` and `changelog.md` (3.0.0) for what changed.

## Nodes

`router-1` holds the job table. `worker-N` nodes lease jobs and run them on
the GPU (GPU API v2). The deployment capacity was 8 GPU units.

## Leases

- The router grants a lease of `LEASE_S` = 5.0 s. Workers renew every 1.0 s.
- The worker computes its lease expiry as `reply_received_at + LEASE_S`, on its
  own clock. No drift allowance is applied: nodes were assumed to be NTP-synced
  to within a few milliseconds.
- When a lease expires on the router's clock, the job is immediately returned to
  the queue and may be granted to another worker.
- Epochs are kept in router memory and restart from 1 when the router restarts.

## GPU operations

- Each lease draws a fresh op_id (`<job_id>-<random>`) from the worker's `rng`,
  so a re-granted job starts a new GPU operation.
- On submit timeout the worker resubmits immediately with a new op_id.
- The worker releases the operation when it finishes and reports the job done.
  The router counts the units as free as soon as the worker reports, since the
  GPU v2 `release` call freed capacity synchronously.

## Cancellation

`cancel` marks the job CANCELLED in the router at once and asks the holder to
cancel the GPU operation. If the operation finishes first its result is
discarded.

## Idempotency

Client submits are not deduplicated before 2.4; clients must not retry
`submit` after a timeout.
