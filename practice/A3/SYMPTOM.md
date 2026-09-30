# A3: incident report

| | |
| --- | --- |
| Service | jobrouter: asynchronous GPU job router with leases and retries |
| Case | A3 (3 independent issues, see the backlog below) |
| Budget | 900 s wall clock, $9.00 model spend |
| Evidence | `logs/` from `python -m sim replay --scenario public --seed 13` |
| Other public seeds | 11, 12, 14, 15 |

Only files under `src/` are graded. The invariants are in `INVARIANTS.md`;
check a replay with `python -m sim check --trace <out>/trace.jsonl`.

---

On-call backlog from Tuesday afternoon. It was a rough one: router-1 restarted
several times, it lost the network to half of the workers twice for a few
seconds and to all of them once for about 15 s (the workers were bounced
during the long one), and the GPU failed more operations than usual. Three
separate reports came in; logs from one replay of the afternoon are attached.

1. **Tenant C billing dispute.** Their long jobs, cancelled during the long
   outage, show CANCELLED to them, but platform accounting shows the GPU
   operations for those same jobs finished SUCCEEDED and were never released.
   They were billed for the run and never got a result.
2. **Big jobs never start after the router restarts.** Tenant B's 7-unit jobs
   sit QUEUED for the rest of the afternoon even once the GPU is quiet;
   their clients are still polling when the run ends. Small jobs keep going.
3. **Registration flood during the outage.** The workers that restarted while
   router-1 was unreachable kept sending registration attempts about once a
   second each until it came back, and platform networking flagged the
   traffic from them.
