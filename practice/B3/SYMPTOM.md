# B3: incident report

| | |
| --- | --- |
| Service | shardmr: sharded map-reduce over the C3 GPU service |
| Case | B3 (more than one issue may be involved) |
| Budget | 900 s wall clock, $6.00 model spend |
| Evidence | `logs/` from `python -m sim replay --scenario public --seed 12` |
| Other public seeds | 11, 13, 15, 17 |

Only files under `src/` are graded. The invariants are in `INVARIANTS.md`;
check a replay with `python -m sim check --trace <out>/trace.jsonl`.

---

Staging soak from last night (`scenarios/public.yaml`). The staging cell runs 4 workers, and the soak restarts worker processes now and then and adds clock skew and GC-style pauses. Two items for the on-call backlog, possibly unrelated:

1. **Workers flapping after a process restart.** When a worker process restarts it is back within a couple of seconds and registers fine. A few seconds later coord declares it dead, then again and again for a long time afterwards, so it hardly finishes any work.
2. **Healthy workers declared dead.** coord logs `worker_dead` for workers that never crashed, and the same worker is hit again every few seconds for the whole run. The worker's own log shows no crash or restart. All of its tasks get revoked and re-run each time. This runs within the skew and pause limits the deployment is sized for.

Each item shows up on some seeds and not others.
