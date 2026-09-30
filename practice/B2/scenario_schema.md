# shardmr scenario schema

Scenarios follow the c3sim format (`runtime/README.md`). shardmr needs:

```yaml
gpu: {capacity: 12, release_delay: {dist: uniform, low: 0.05, high: 0.3}}
nodes:
  - {name: coord, class: "shardmr.coordinator:Coordinator"}
  - {name: "worker-{i}", count: 4, class: "shardmr.worker:Worker"}
client: {target: coord}
client_retry: {timeout: 2.0, max_attempts: 5, backoff: 0.5, retry_on: [UNAVAILABLE]}
```

The coordinator must be named `coord`. Workers may take
`config: {coordinator: <name>}` but the default is `coord`. There is no
`service:` configuration; every constant is in `src/shardmr/config.py`.

## Workload actions

| Action | Arguments | Behaviour | Outcomes |
| --- | --- | --- | --- |
| `submit_job` | `tenant`, `job_id`, `shards`, `records`, optional `units` (default 1), `duration` (default 1.0), `poll` (client poll interval, default 1.0) | Calls `submit_job` with the `client_retry` policy, records a client `accepted` event, then polls `job_status` every `poll` seconds until the job is terminal | `ok` (SUCCEEDED; `result` is the status reply including `result`), `failed` (FAILED), `cancelled` (CANCELLED), `error` (submit rejected, e.g. `CONFLICT`, `INVALID`), `timeout` (submit never answered) |
| `cancel_job` | `tenant`, `job_id` | One `cancel_job` call with the retry policy | `ok` with `result: {state}`, `timeout`, `error` |
| `job_status` | `tenant`, `job_id` | One `job_status` call (generic action) | `ok`, `error` (`NOT_FOUND`), `timeout` |

Resubmitting the same `(tenant, job_id)` with the same parameters is allowed
and exercises submit idempotency. A `cancel_job` scheduled before the job's
`submit_job` exercises cancel-before-submit.

## Scenarios

| File | Purpose |
| --- | --- |
| `base.yaml` | Normal operation: light loss, 2% GPU failures, one worker crash |
| `soak.yaml` | Clean-baseline soak (T15): `faults/soak.yaml`, every fault type |
| `stragglers.yaml` | Heavy-tailed GPU latency, ample capacity: speculative duplicates race to commit |
| `public-template.yaml` | Starting point for a case's `public.yaml` |

## Fault family (`scenarios/faults/`)

Swap with `python -m sim replay --scenario public --faults scenarios/faults/<f>.yaml`.

| File | Faults |
| --- | --- |
| `calm.yaml` | GPU latency only |
| `clocks.yaml` | Drift within 200 ppm, offsets within 0.5 s, pauses up to 2.0 s |
| `crashes.yaml` | Random worker and coordinator crashes and restarts |
| `gpu.yaml` | GPU failures, lost submit replies, `UNAVAILABLE` after acceptance |
| `network.yaml` | Loss, duplication, reordering, coordinator partitions |
| `stragglers.yaml` | Heavy-tailed GPU latency, slow reordering network |
| `soak.yaml` | Everything at once, at soak intensity |

## Extra `expect` keys

`max_failed_jobs: N`, `max_timed_out_jobs: N`. See `INVARIANTS.md`.
