# shardmr: sharded map-reduce over the C3 GPU service

shardmr runs map-reduce jobs on the simulated C3 GPU. A client submits a job of
N shards; one coordinator splits it into map tasks, hands them to workers in
batches, keeps the first committed output of every shard, and runs a single
reduce over the committed outputs of one job attempt. Stragglers get a
speculative duplicate; the first commit wins.

Read `service-docs/design-overview.md` first, then
`service-docs/operational-notes.md` for every number the code relies on.
The platform contracts are in the corpus under `platform/`.

## Layout

```
src/shardmr/
  config.py        every contract constant (timeouts, budgets, limits)
  ids.py           identifiers and durable key layout
  store.py         durable job records and job states
  commit.py        shard commit log (first commit wins)
  membership.py    worker registration, heartbeats, epochs, failure detection
  scheduler.py     task queue, batching, reconciliation, speculation
  coordinator.py   the coordinator node: client API and worker protocol
  reduce.py        the reduce stage
  worker.py        the worker node: registration, heartbeats, batches, restart recovery
  executor.py      one map task: GPU op, commit, release
  gpu_client.py    caller-side GPU driver (gpu-api-v3 retry discipline)
  kernels.py       map/reduce kernel models (deterministic)
invariants.py      trace invariants (see INVARIANTS.md)
scenarios.py       workload actions (see scenario_schema.md)
scenarios/         base, soak, stragglers, public-template, faults/
tests/             public tests (pytest)
service-docs/      design, API, runbooks, operational notes, changelog
```

## Running

From a case root:

```
python -m sim replay --scenario public --seed 11 --out /tmp/run
python -m sim check --trace /tmp/run/trace.jsonl
python -m sim replay --scenario public --faults scenarios/faults/crashes.yaml --seed 3
python -m pytest -q tests
```

Logs land in `/tmp/run/logs/<node>.log`, one `event=` line per state
transition. Log timestamps are node-local.
