# shardmr invariants

`invariants.py` implements `check(events) -> [Violation]` over the trace only
(`python -m sim check --trace DIR/trace.jsonl`). Liveness is judged after the
scenario's `quiesce_at`, within `liveness_window` virtual seconds (default 30).
`expect` blocks in scenarios add per-scenario assertions on top.

## Common

- **I0, no effect without an operation.** Every `gpu_effect` record names an
  `op_id` that an earlier `accepted` effect created.
- **I1, capacity.** At no point does the GPU's committed unit count exceed its
  capacity.
- **gpu_once.** No `op_id` is started twice (a platform sanity check).
- **I2, liveness.** Every client action that starts has an `end` record, and
  every action started by `quiesce_at + liveness_window` ends by then.
- **I3, goodput.** Total `msg_send` records are at most 3x the reference run's
  count on the same seed. The grader supplies the reference count
  (`--reference-messages N`); without it I3 is not checked.

## shardmr-specific

A *commit grant* is either a `shard_committed` log record on `coord` or a
`commit` RPC reply from `coord` with `committed: true` (paired with its request
by `rpc_id`). Grants are keyed by `(job, attempt, shard)`, where `job` is
`<tenant>:<job_id>`.

- **B1, each shard's output is committed exactly once.** For every
  `(job, attempt, shard)`, all commit grants name the same task. In addition,
  each map task runs as at most one GPU operation: all accepted map operations
  whose payload names task T share one `op_id`. (That a shard is committed at
  least once before its job succeeds is covered by B2.)
- **B2, the reduce reads only committed outputs of one attempt.** For every
  accepted reduce operation, with payload attempt `a` for job `j`:
  - every input's `attempt` is `a`;
  - every input's shard has a commit grant in `(j, a, shard)` at or before the
    reduce was accepted, and the input names the task of the earliest such
    grant;
  - where the granting commit request is in the trace, the input's output
    equals the output that request carried;
  - no shard appears twice, and every shard `0 .. shards-1` of the job appears.
- **B3, the final result equals the reference.** For every `submit_job` action
  that ends `ok`, the returned `result` equals the reference result recomputed
  in `invariants.py` from `(tenant, job_id, shards, records)` alone,
  independently of the service code: per shard, the count, sum and digest of
  the shard's deterministic input records, combined as
  `{shards, count, sum, checksum}`.

## Scenario `expect` keys

Built in (see `c3sim/check.py`): `all_actions_resolved_by`,
`all_jobs_complete_by`, `outcomes_max`, `outcomes_min`, `log_events_max`,
`log_events_min`, `messages_max`, `no_unhandled_exceptions`.
Added by `scenarios.py`: `max_failed_jobs: N` and `max_timed_out_jobs: N`
(at most N `submit_job` actions end `failed` / `timeout`).
