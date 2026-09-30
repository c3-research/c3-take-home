# jobrouter scenario schema

Scenarios follow the c3sim format (`runtime/README.md`). This file lists what
is specific to jobrouter.

## Nodes and service settings

```yaml
gpu: {capacity: 12, release_delay: {dist: uniform, low: 0.05, high: 0.5}}
nodes:
  - {name: router-1, class: "jobrouter.router:Router"}
  - {name: "worker-{i}", count: 4, class: "jobrouter.worker:Worker"}
service: {worker_slots: 2}
client: {target: router-1}
```

`service:` keys (all optional):

| Key | Default | Meaning |
| --- | --- | --- |
| `worker_slots` | 2 | Concurrent leases per worker |
| `gpu_capacity` | `gpu.capacity` | Capacity the router accounts against. The scenario loader copies `gpu.capacity` here when it is not set |
| `router` | `router-1` | Router node name, for workers |

## Workload actions

| Action | Arguments | What the client does | Outcome |
| --- | --- | --- | --- |
| `submit_job` | `tenant` (default `default`), `job_id`, `units` (default 1), `duration` (default 1.0) | `submit` with the scenario's `client_retry` policy, then polls `status` every 1.0 s until the job is terminal | `ok` (SUCCEEDED), `failed` (FAILED), `cancelled` (CANCELLED); `error` with `code` if submit was rejected; `timeout` if every submit attempt timed out |
| `cancel_job` | `tenant`, `job_id` | `cancel` with the retry policy, then polls `status` until terminal (unless the cancel reply was already terminal) | `ok`, with `result.state` the job's final state |
| `job_status` | `tenant`, `job_id` | One `status` call (retry policy applies) | `ok` with `result.state`; `error` `NOT_FOUND` if never submitted |
| `stats` | none | One `stats` call | `ok` with the router's gauges and counters |

A `submit_job` records a custom client event `accepted` once the submit is
acknowledged, and `cancel_job` records `cancel_acknowledged`.

## Extra `expect` keys

| Key | Meaning |
| --- | --- |
| `max_capacity_waits: N` | At most N `gpu_capacity_wait` log records |
| `max_fenced: N` | At most N `lease_fenced` log records |
| `min_jobs_succeeded: N` | At least N jobs end SUCCEEDED |

Built-in keys (`all_actions_resolved_by`, `all_jobs_complete_by`,
`outcomes_max`, `log_events_max`, `no_unhandled_exceptions`, ...) are listed in
`c3sim/check.py`.

## Shipped scenarios

| File | Purpose |
| --- | --- |
| `base.yaml` | Steady load, light network loss and GPU faults |
| `soak.yaml` | Clean-baseline gate: every fault type (`faults/soak.yaml`) |
| `public-template.yaml` | Starting point for a case's `public.yaml` |
| `faults/network.yaml` | Delay, loss, duplication, reordering, partitions |
| `faults/crashes.yaml` | Worker and router crashes and restarts |
| `faults/clocks.yaml` | Drift and offset up to the documented bound, pauses |
| `faults/gpu.yaml` | GPU failures, lost replies, UNAVAILABLE-after-accept |
| `faults/soak.yaml` | All of the above together |

`--faults FILE` replaces a scenario's whole `faults` block.
