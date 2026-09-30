> SUPERSEDED by gpu-api-v3.md. Kept for reference.

# C3 GPU Service API, version 1

Status: superseded. Do not build against this version. See `changelog.md` for
what changed.

The GPU service is a platform node named `gpu`. It accepts work, runs it, and
reports completion.

## Concepts

- **Job.** A unit of GPU work. The GPU assigns each job a `job_id` when it is
  accepted.
- **Slots.** The GPU runs a fixed number of jobs at once. A job occupies one slot
  from acceptance until it finishes.

## Methods

### `run`

Request: `{tenant: str, duration: float, payload: dict}`.
Reply: `{job_id: str}`.

- Every call creates a new job. Retrying a `run` whose reply was lost starts a
  second job.
- `RpcError("BUSY")` when all slots are taken.

### `poll`

Request: `{job_id: str}`.
Reply: `{done: bool, ok?: bool, result?}`.

### `abort`

Request: `{job_id: str}`. Reply: `{}`.

The job stops and its slot is freed immediately.

## Capacity

A slot is freed the moment its job finishes, fails or is aborted. There is no
separate release step.

## Errors

`BUSY` is the only error code. A timeout means the GPU did not receive the
request, so callers may retry `run`.
