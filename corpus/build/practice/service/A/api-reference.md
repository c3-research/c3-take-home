# jobrouter: API reference

Status: CURRENT (jobrouter 3.2).

All methods are platform RPCs on `router-1`. Payloads and replies are JSON
objects. Errors are raised as `RpcError(code, message)`. Every state-changing
method is idempotent, as `platform/idempotency.md` requires.

## Client API

The idempotency scope of every client method is `(tenant, job_id)`. The
deduplication record is the job record itself; it is written to disk before
the reply and is never deleted, so the key's lifetime is the lifetime of the
deployment.

### `submit {tenant, job_id, units, duration}`

Reply: `{accepted: true, state}`.

- `tenant`, `job_id`: non-empty strings. `units`: integer >= 1. `duration`:
  seconds, number >= 0 (default 1.0).
- A new key creates a QUEUED job.
- A repeated key with the same `units` and `duration` returns the job's current
  state and does nothing else.
- A repeated key with different `units` or `duration`: `RpcError("MISMATCH")`.
- A key that was cancelled before it was ever submitted (see `cancel`) is
  accepted; the job is, and stays, CANCELLED.
- `units` above the deployment capacity: `RpcError("INVALID")`.
- Malformed request: `RpcError("INVALID")`.

### `status {tenant, job_id}`

Reply: `{tenant, job_id, state, attempt, cancel_requested, result?, op_id?}`.

- `state`: QUEUED, LEASED, SUCCEEDED, FAILED or CANCELLED.
- `result` and `op_id` are present only when `state` is SUCCEEDED: the GPU
  result of the operation that succeeded, and its op_id.
- `RpcError("NOT_FOUND")` if the key was never submitted.

### `cancel {tenant, job_id}`

Reply: `{state}`, where `state` is a terminal job state, or `CANCELLING` when the
job's current attempt has been leased and the cancel is being carried out by
its holder. Poll `status` to learn the final state, which is CANCELLED or, if
the GPU operation finished first, SUCCEEDED. Repeating a cancel is harmless.
Cancelling an unknown key records a tombstone so that a later submit of that
key never runs.

## Holder API (workers)

### `hello {boot}` -> `{ok: true}`

Registers a worker incarnation. A boot count higher than any seen before for
the worker revokes the leases held by its earlier incarnations.

### `acquire {req_id, boot, slot}` -> `{lease: grant | null}`

Leases the next dispatchable job, or returns `{lease: null}` when there is none.
Retries of one request reuse its `req_id`; the router answers a repeated
`req_id` with the grant it already made, if that lease is still current.
`RpcError("STALE_BOOT")` if `boot` is older than the newest incarnation seen.

`grant = {key, tenant, job_id, units, duration, attempt, epoch, op_id, mode,
lease_s, expires_at}`. `mode` is `run`, or `cancel` when a cancel has been
requested. `expires_at` is on the router's clock and is informational only:
holders never compare it with their own clock.

### `renew {key, epoch, boot}` -> `{ok: true, cancel, expires_at}`

Extends the lease by `LEASE_S` from the router's processing time. `cancel` is
true once a cancel has been requested for the job.
Errors: `FENCED` (not the current epoch, holder or boot), `EXPIRED` (the lease
already ran out on the router's clock).

### `report {key, epoch, boot, op_id, state, result?}` -> `{ok: true, state}`

Records the terminal state of the attempt's GPU operation. `op_id` must be the
job's current op_id and `state` a terminal GPU state, else `INVALID`. A report
that does not carry the current epoch, holder and boot, or arrives after the job
is terminal, gets `FENCED`, except that an exact repeat of the report that was
applied (same epoch and op_id) returns `{ok: true, state}` again.

## Admin API

### `stats {}`

Returns the router's gauges (`jobs`, `queued`, `leases`, `releasing`,
`reserved`, `capacity`, `workers`, `states`) and its counters since boot
(`counts`: submitted, duplicates, cancels, grants, renewals, expiries,
revocations, fenced, reports, releases).

## GPU usage

jobrouter uses `submit`, `status` and `cancel` from workers, and `release` and
`status` from the router, as specified in `platform/gpu-api-v3.md`. The GPU
`payload` of each submit is `{job, attempt, epoch, holder}`; the GPU ignores the
fields of repeated submits, so the payload recorded is the first holder's.
