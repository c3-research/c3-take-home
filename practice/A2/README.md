# jobrouter: asynchronous GPU job router with leases and retries

jobrouter accepts GPU jobs from tenants, leases each job to a worker, and runs
the job's work as an operation on the C3 platform GPU service. It runs on the
C3 runtime (simulated here by `c3sim`): the network loses, duplicates, delays
and reorders messages, clocks drift, nodes pause, crash and restart, and the
GPU has its own documented quirks. The service documentation (design, API,
operational constants, runbooks) is in the corpus under `service/A/`, and the
platform contracts it follows are under `platform/`.

## Nodes

- `router-1` (`jobrouter.router:Router`): the job table, lease grants and
  fencing, capacity accounting and releases. All job state is on disk, one
  record per job.
- `worker-N` (`jobrouter.worker:Worker`): stateless; leases jobs and runs them
  on the GPU, two at a time.
- `gpu`: the platform GPU service (`platform/gpu-api-v3.md`).
- `client`: the scenario's workload driver.

## Source layout (`src/jobrouter/`)

| Module | Responsibility |
| --- | --- |
| `config.py` | Every deployment constant (lease length, drift budget, timeouts, capacity, retry limits) |
| `models.py` | Job records, states, op_id derivation, request validation |
| `store.py` | Durable job table and dispatch queue (router) |
| `leases.py` | Grantor lease table (router) and holder lease view (worker) |
| `dispatch.py` | Which queued job is leased next |
| `capacity.py` | GPU capacity ledger (router) |
| `releaser.py` | Releasing finished GPU operations and waiting for ReleaseComplete (router) |
| `recovery.py` | Router boot recovery from disk |
| `metrics.py` | Counters, `router_stats` log line, `stats` admin call |
| `router.py` | Router node: client, holder and admin RPC handlers; lease monitor |
| `worker.py` | Worker node: registration, acquire loop per slot |
| `executor.py` | Running one lease: submit, poll, cancel, report, renew |
| `gpu_client.py` | GPU RPC wrapper with the platform's retry rules |
| `retry.py` | Backoff helpers |

## Job lifecycle in one paragraph

A client `submit`s `(tenant, job_id, units, duration)`. The router stores the
job (QUEUED) and, when capacity allows, grants a lease to a worker slot that
calls `acquire`, bumping the job's epoch. The worker submits the attempt's GPU
operation (`op_id = <tenant>/<job_id>/a<attempt>`), polls it to a terminal
state while renewing its lease, and reports the result under its epoch. The
router records the outcome, releases the GPU operation and frees its units once
the GPU confirms the release completed. The client polls `status` until the job
is SUCCEEDED, FAILED or CANCELLED.

## Running

From the case root:

```
python -m sim replay --scenario public --seed 11 --out /tmp/run
python -m sim check --trace /tmp/run/trace.jsonl
python -m pytest -q tests
```

Invariants are defined in `INVARIANTS.md`; workload actions in
`scenario_schema.md`.
