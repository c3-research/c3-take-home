# C3 GPU Service API, version 3

Status: CURRENT. Supersedes `gpu-api-v2.md` and `gpu-api-v1.md`. Differences
from earlier versions are listed in `changelog.md`.

The GPU service is a single platform node named `gpu`. Services reach it with
ordinary platform RPCs (`rpc("gpu", method, payload, timeout)`), so everything in
`runtime-v3.md` about lost, duplicated, delayed and reordered messages applies to
GPU calls too.

## 1. Concepts

- **Operation.** A unit of GPU work, identified by a caller-chosen `op_id`
  string. An operation runs at most once, however many times it is submitted.
- **Units.** Every operation reserves `units` of the GPU's fixed capacity.
  Capacity is configured per deployment. There is no method to query it: each
  service's documentation states the capacity it is deployed against.
- **Committed units.** Units are committed when an operation is accepted and
  stay committed until the operation has been released *and the release has
  completed* (section 3.4). Finishing, failing or being cancelled does not free
  units by itself.
- **Tenant.** A free-form label carried with the operation for accounting. The
  GPU does not enforce per-tenant limits.

## 2. Operation states

```
            submit accepted
                  |
                  v
   cancel     PENDING ----------> RUNNING ----------> SUCCEEDED
  +---------+    |                  |    \
  v         |    | cancel           |     `---------> FAILED
CANCELLED <-+----+                  |
    ^                               |
    +---------- cancel -------------+
```

| State | Meaning | Terminal |
| --- | --- | --- |
| `PENDING` | Accepted, units committed, not started yet | no |
| `RUNNING` | Executing | no |
| `SUCCEEDED` | Finished; `result` is present | yes |
| `FAILED` | Finished without a result. A failed operation is not retried by the GPU | yes |
| `CANCELLED` | Stopped by `cancel` before it finished | yes |

An operation runs for its requested `duration` plus a platform scheduling
latency. The latency varies with load and per deployment. The API gives no
upper bound on it, so callers must poll rather than assume a completion time.

Terminal states never change. In particular a `cancel` that arrives after an
operation reached `SUCCEEDED` or `FAILED` does not change its state.

## 3. Methods

All payloads and replies are JSON objects. Errors are raised as
`RpcError(code, message)`. Only the codes listed below are used.

### 3.1 `submit`

Request:

```
{op_id: str, tenant: str, units: int >= 1, duration: float (seconds), payload: dict}
```

Reply on acceptance: `{accepted: true}`.

- **Idempotent by `op_id`.** If an operation with this `op_id` already exists,
  nothing new is created or run, no further units are committed, and the reply
  is `{accepted: true, state: <current state>, result?}`. The fields of the
  repeated request are ignored; the original request is authoritative. This
  holds for the lifetime of the deployment: `op_id`s never expire and are never
  reused by the platform.
- `RpcError("CAPACITY")`: accepting the operation would take committed units
  above capacity. Nothing was created, and the `op_id` is still unused, so the
  same `op_id` can be submitted again later.
- `RpcError("UNAVAILABLE")`: see section 4. **The operation may have been
  accepted.**
- A timeout (`RpcTimeout`) tells the caller nothing: the request may have been
  lost, or the operation may have been accepted and the reply lost.

### 3.2 `status`

Request: `{op_id: str}`.
Reply: `{state: "PENDING"|"RUNNING"|"SUCCEEDED"|"FAILED"|"CANCELLED", result?, released: bool}`.

- `result` is present only when `state` is `SUCCEEDED`.
- `released` becomes `true` once the operation's release has completed and its
  units are free again (section 3.4).
- `RpcError("NOT_FOUND")`: the GPU has not accepted an operation with this
  `op_id` *so far*. Because messages can be delayed and reordered, a submit sent
  earlier may still arrive after this reply. `NOT_FOUND` therefore does not
  prove that the operation will never exist; it only makes it safe to submit
  again **with the same `op_id`**.

### 3.3 `cancel`

Request: `{op_id: str}`. Reply: `{ok: true}`.

- A `PENDING` or `RUNNING` operation moves to `CANCELLED` and produces no
  further effects.
- On a terminal operation, `cancel` is a no-op and still replies `{ok: true}`.
  The reply does not say whether the cancel took effect: call `status` to learn
  whether the operation ended `CANCELLED` or finished first.
- Cancelling does not free units. A cancelled operation must still be released.
- `RpcError("NOT_FOUND")` if no such operation has been accepted (see 3.2 for
  what that does and does not mean).

### 3.4 `release`

Request: `{op_id: str}`. Reply: `{ok: true}`.

- Valid only on a terminal operation. Every accepted operation, including
  `FAILED` and `CANCELLED` ones, must eventually be released, or its units stay
  committed forever.
- `RpcError("NOT_TERMINAL")` if the operation is `PENDING` or `RUNNING`.
  Cancel it first, wait for a terminal state, then release.
- `RpcError("NOT_FOUND")` if no such operation has been accepted.
- **Release is asynchronous.** The `{ok: true}` reply means the release was
  recorded, not that capacity is free. The GPU frees the units a short, variable
  time later and then emits `ReleaseComplete`:
  - `status(op_id).released` becomes `true`, and
  - the GPU sends a one-way message, method `release_complete`, payload
    `{op_id}`, to the node that issued the first `release`. Like any message it
    can be lost, delayed or duplicated, so it is a hint, not a guarantee.
  Until `ReleaseComplete`, the units still count against capacity, and a
  `submit` that needs them fails with `CAPACITY`. Callers that track free
  capacity themselves must not count released units as free until they have
  observed `released: true` or a `release_complete` message.
- Repeating `release` is harmless: it replies `{ok: true}` and never frees the
  units twice.

## 4. Platform quirk: `UNAVAILABLE` after acceptance

Under load, `submit` can reply `RpcError("UNAVAILABLE")` even though the GPU
**accepted** the operation. The operation is then committed and will run
normally; only the reply is wrong.

Callers must therefore treat `UNAVAILABLE` exactly like a timeout, an unknown
outcome:

1. Do not treat the operation as failed, and do not free any capacity or
   bookkeeping associated with it.
2. Call `status(op_id)` before doing anything else with the operation.
3. If `status` returns a state, the operation exists: continue from that state.
4. If `status` returns `NOT_FOUND`, submit again **with the same `op_id`**.
   Never resubmit an `UNAVAILABLE` operation under a new `op_id`: if the
   original was accepted, both would run.

A retry loop that resubmits the same `op_id` is safe (submit is idempotent),
but one that allocates a fresh `op_id` per attempt runs the work twice whenever
this quirk fires.

## 5. What the GPU records

The GPU records each effect (operation started, operation finished with its
final state, units freed) in the platform trace. Platform correctness checks
use these records to verify that each operation ran at most once and that
committed units never exceeded capacity. Callers cannot read these records.

## 6. Checklist for callers

- Choose `op_id`s so that two submissions that must not both run share one
  `op_id`. A crash-restarted caller must be able to recompute or reload the
  `op_id` (persist it to disk before submitting).
- Treat `UNAVAILABLE` and timeouts as unknown outcomes; resolve with `status`.
- Poll `status` to learn completion; there is no completion callback.
- Release every terminal operation exactly when you no longer need its result,
  and wait for `ReleaseComplete` before reusing its units in your own accounting.
- `cancel` is a request, not a guarantee: an operation can finish first.
