# B2: incident report

| | |
| --- | --- |
| Service | shardmr: sharded map-reduce over the C3 GPU service |
| Case | B2 (3 independent issues, see the backlog below) |
| Budget | 900 s wall clock, $9.00 model spend |
| Evidence | `logs/` from `python -m sim replay --scenario public --seed 1` |
| Other public seeds | 3, 5, 6, 7 |

Only files under `src/` are graded. The invariants are in `INVARIANTS.md`;
check a replay with `python -m sim check --trace <out>/trace.jsonl`.

---

On-call backlog, shardmr, week 39. Three open items, probably unrelated.

1. After a coord/worker network blip of about four seconds (short enough that the
   worker is not declared dead), some single-shard jobs never finish: MAPPING with
   committed=0, and nothing is dispatched for them again. The worker's heartbeats look
   healthy afterwards.
2. Soak on the jittery network profile (reordering on, sub-second blips, no loss
   otherwise): coord keeps re-queuing shards whose tasks are still running on a healthy
   worker (task_finished outcome=missing_from_heartbeat); the originals get cancelled
   on the GPU and the maps run again. No GPU failures, no worker deaths.
3. With plain packet loss some jobs hang for good, several of them in REDUCING. The op
   behind each one logs gpu_submit_unknown and after that only status polls.
