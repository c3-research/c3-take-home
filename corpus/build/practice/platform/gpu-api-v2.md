> SUPERSEDED by gpu-api-v3.md. Kept for reference.

# C3 GPU Service API, version 2

Status: superseded. Do not build against this version. See `changelog.md` for
what changed in version 3.

The GPU service is a platform node named `gpu`. Version 2 introduced
caller-chosen operation identifiers and unit-based capacity.

## Concepts

- **Operation.** A unit of GPU work identified by a caller-chosen `op_id`.
- **Units.** Each operation reserves `units` of the GPU's capacity.
- **Deduplication window.** The GPU remembers each `op_id` for 600 seconds after
  the operation reaches a terminal state. A submit with a remembered `op_id`
  returns the existing operation. After the window, the same `op_id` starts a new
  operation.

## Operation states

`PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `CANCELLED`. The GPU retries a
`FAILED` operation once on its own before reporting `FAILED`.

## Methods

### `submit`

Request: `{op_id, tenant, units, duration, payload}`. Reply: `{accepted: true}`.

- `RpcError("CAPACITY")` when capacity is exhausted.
- `RpcError("UNAVAILABLE")` when the GPU is overloaded. The operation was **not**
  accepted, so the caller may retry immediately, with the same or a new `op_id`.

### `status`

Request: `{op_id}`. Reply: `{state, result?}`. An unknown `op_id` returns
`{state: "UNKNOWN"}`.

### `cancel`

Request: `{op_id}`. Reply: `{cancelled: bool}`: `true` if the operation was
stopped before finishing.

Cancelling frees the operation's units.

### `release`

Request: `{op_id}`. Reply: `{ok: true}`.

Frees the operation's units. When `release` returns, the units are available to
the next `submit`. Release is required only for `SUCCEEDED` and `FAILED`
operations.

## Retrying

Callers should retry on `UNAVAILABLE` and on timeouts. Using the same `op_id` is
recommended but not required on `UNAVAILABLE`.
