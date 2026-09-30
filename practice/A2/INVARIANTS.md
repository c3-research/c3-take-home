# jobrouter invariants

`python -m sim check --trace DIR/trace.jsonl` evaluates these on a replay's
trace (`invariants.py`), then the scenario's `expect` block. A violation is
`{"invariant", "t", "detail"}`. Checkers read only the trace.

Terms used below:

- **Lease** `(job, epoch)`: created by the router's `lease_granted` log record
  (fields `key`, `epoch`, `holder`, `holder_boot`, `lease_s`). It is
  acknowledged at its grant and at every `lease_renewed` and `lease_restored`
  record for the same `(key, epoch)`.
- **Grantor validity** of a lease: from its grant until the last
  acknowledgement plus `lease_s` converted to true time with the router's
  actual clock rate (the trace's `clock_skew` fault record for the router gives
  `drift_ppm`), cut short by the router's `report_applied` record for the
  lease, or by a `lease_revoked` record if the holder crashed in between.
- **Holder actions**: messages a worker *sends* under a lease: GPU `submit`
  (the lease is named in the submit payload's `job` and `epoch`), GPU `cancel`
  (`job`, `epoch`) and router `report` (`key`, `epoch`). The send time is the
  `msg_send` record's `t`.
- **Accepted job**: one with a router `job_accepted` record.

## Common invariants

- **I0 no effect without an operation.** Every `gpu_effect` references an
  op_id that has an `accepted` effect earlier in the trace.
- **I1 capacity.** No `gpu_effect` record has `committed > capacity`.
- **I2 liveness.** Every client action has an `end` record, and every action
  that started by `quiesce_at + liveness_window` ended by that time.
- **I3 goodput.** Total `msg_send` records are at most 3x the reference run's
  count on the same seed (applied by the grader with `--reference-messages`).
- **gpu_once** (sanity): no op_id has two `started` effects.

## A1: no job has effects under two overlapping leases

For every job:

1. No GPU `submit` or `cancel` sent under epoch `e`, and no `report_applied`
   for epoch `e`, happens after a lease with a higher epoch was granted for the
   same job.
2. For consecutive leases `a` and `b` (by epoch) that both had effects, `b` is
   not granted before the grantor validity of `a` ends.

## A2: every accepted job has exactly one outcome

Using the GPU's `accepted` effects (whose `payload.job` names the job), the GPU
`finished` states, and the router's first `job_terminal` record per job:

- No job has more than one operation that finished SUCCEEDED.
- Every job accepted by `quiesce_at` reaches `job_terminal`.
- SUCCEEDED: exactly one operation SUCCEEDED, and it is the one the router
  recorded (`op_id` field).
- CANCELLED: no operation SUCCEEDED, and no operation of the job has an
  `accepted`, `started` or `finished` effect after the router's `job_terminal`
  record (the cancel acknowledgement).
- FAILED: no operation SUCCEEDED and at least `MAX_GPU_ATTEMPTS` (8) operations
  finished FAILED.
- Every `submit_job` / `cancel_job` client end record that reports a terminal
  state agrees with the router's terminal state for that job, and a SUCCEEDED
  result's `digest` is the digest of the operation the router recorded.

## A3: a holder never acts after its lease expires

For every holder action `(t, sender, job, epoch)`:

- the lease `(job, epoch)` exists;
- the sender is the lease's holder, and is still the incarnation (boot count)
  the lease was granted to;
- `t` is no later than the lease's grantor expiry as acknowledged up to `t`:
  the latest acknowledgement at or before `t` plus `lease_s` in true time.

Holders stay inside this bound by measuring the lease from their send time and
subtracting the documented drift budget (`LEASE_S * DRIFT_RATE + MAX_DRIFT_S`,
see the service's operational notes), so A3 holds for any clock drift up to
the documented 1000 ppm.
