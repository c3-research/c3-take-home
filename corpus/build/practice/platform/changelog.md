# Platform changelog

Newest first. Current documents: `gpu-api-v3.md`, `runtime-v3.md`,
`leases-and-fencing.md`, `idempotency.md`. Superseded: `gpu-api-v2.md`,
`gpu-api-v1.md`.

## GPU API v3 (current)

Breaking changes from v2:

- **`op_id` never expires.** v2 forgot an `op_id` 600 seconds after the
  operation finished, and a later submit with that `op_id` started new work. In
  v3 an `op_id` identifies one operation for the lifetime of the deployment, and
  a repeated submit returns `{accepted: true, state, result?}` without running
  anything.
- **`UNAVAILABLE` no longer means "not accepted".** Under load, `submit` can
  return `UNAVAILABLE` for an operation it accepted. Callers must call
  `status(op_id)` before resubmitting, and must resubmit only with the same
  `op_id`. The v2 advice ("retry immediately, with the same or a new `op_id`")
  is unsafe in v3. See `gpu-api-v3.md` section 4.
- **Release is asynchronous.** In v2 the units were free when `release`
  returned. In v3 the reply only records the release; the units are freed later,
  signalled by `ReleaseComplete` (`status.released: true` and a
  `release_complete` message to the releaser).
- **Every terminal operation must be released,** including `CANCELLED` ones.
  In v2, `cancel` freed units by itself. In v3, `cancel` never frees units, and
  `release` on a non-terminal operation fails with `NOT_TERMINAL`.
- **`cancel` reply is `{ok: true}`** and does not say whether the cancel won the
  race with completion. v2 returned `{cancelled: bool}`. Use `status`.
- **Unknown `op_id`** now raises `RpcError("NOT_FOUND")` from `status`, `cancel`
  and `release`. v2's `status` returned `{state: "UNKNOWN"}`.
- **No automatic retry of failed operations.** v2 retried a `FAILED` operation
  once internally. In v3, `FAILED` is final for that `op_id`.
- `status` gained the `released` field.

Unchanged: the five operation states, unit-based capacity, the `CAPACITY` error
(which does not consume the `op_id`), and the fact that there is no capacity
query method.

## GPU API v2 (superseded)

Changes from v1:

- Caller-chosen `op_id` replaced GPU-assigned `job_id`, making `submit`
  idempotent within a 600-second deduplication window.
- Unit-based capacity replaced fixed slots. `BUSY` became `CAPACITY`.
- Added the `UNAVAILABLE` error for overload.
- Added an explicit `release` step for finished operations.
- `run`/`poll`/`abort` were renamed `submit`/`status`/`cancel`. `poll`'s
  `{done, ok}` became the five-state `state` field.

## GPU API v1 (superseded)

Initial version: `run`, `poll`, `abort`; one job per slot; slots freed on
completion; no idempotency.

## Runtime v3 (current)

`runtime-v3.md` describes the execution environment (network, clocks, crashes,
disk). Its guarantees match the simulator that runs every C3 service. Earlier
runtime notes were internal and are not published.
