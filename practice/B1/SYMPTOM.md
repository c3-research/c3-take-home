# B1: incident report

| | |
| --- | --- |
| Service | shardmr: sharded map-reduce over the C3 GPU service |
| Case | B1 (one reported problem) |
| Budget | 900 s wall clock, $3.00 model spend |
| Evidence | `logs/` from `python -m sim replay --scenario public --seed 12` |
| Other public seeds | 14, 20, 22, 25 |

Only files under `src/` are graded. The invariants are in `INVARIANTS.md`;
check a replay with `python -m sim check --trace <out>/trace.jsonl`.

---

The nightly audit on the shardmr batch pool (`python -m sim check` over the night's trace) is failing with B1, "committed by 2 tasks", on some jobs. For each flagged shard, two different workers log `task_committed`. Every job still ends SUCCEEDED and the results look right, so no customer has noticed. We only see it on nights when GPU scheduling is slow and a lot of speculation runs. Seed 12 of `public` reproduces it (tenant A, `agg-1`). Some seeds come back clean.
