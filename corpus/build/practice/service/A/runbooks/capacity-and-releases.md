# Runbook: GPU capacity and releases

Status: CURRENT (jobrouter 3.2).

jobrouter is deployed against a GPU capacity of **12 units** (see
`operational-notes.md`). The GPU has no capacity query; the router's ledger is
the only view of free capacity.

## How units move

1. `capacity_reserved`: the attempt's first lease is granted. Units are
   reserved before any submit can reach the GPU.
2. The worker runs the operation and reports its terminal state
   (`report_applied`). The op is marked `releasing` in the same disk write.
3. `release_requested`: the GPU recorded the router's `release`. **Units are
   still committed on the GPU at this point.**
4. `release_observed via=message|status`: ReleaseComplete seen, either as the
   GPU's `release_complete` message or as `status.released == true` (polled
   every 0.5 s when the message does not arrive).
5. `capacity_freed`: the op is marked freed on disk and its units return to
   the ledger.

An operation that ended FAILED or CANCELLED holds its units until step 5 just
like a SUCCEEDED one (gpu-api-v3 section 3.4).

## Symptoms and checks

- **`gpu_capacity_wait` on workers.** The GPU refused a submit with CAPACITY.
  With a correct ledger this does not happen in steady state. Check that
  `capacity_freed` lines only follow `release_observed` lines for the same
  op_id, never `release_requested` alone.
- **`reserved` stays high while few jobs run.** Look for `release_retry` lines
  (release RPCs timing out) or `release_observed ... polls=N` with large N
  (release_complete messages lost; the status fallback is working but slow).
  A release task runs for every releasing op until it observes completion,
  including across router restarts.
- **Queue not moving although `reserved < capacity`.** See `dispatch_blocked`
  in `stuck-or-slow-jobs.md`.

## Rules

- Never count units as free on the `release` reply.
- Never release an operation that is not terminal (the GPU answers
  `NOT_TERMINAL`). The router only releases after a terminal report.
- Every accepted operation, including FAILED and CANCELLED ones, is released
  exactly once by the router; repeated `release` calls are harmless.
