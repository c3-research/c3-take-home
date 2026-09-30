# A1: incident report

| | |
| --- | --- |
| Service | jobrouter: asynchronous GPU job router with leases and retries |
| Case | A1 (one reported problem) |
| Budget | 900 s wall clock, $3.00 model spend |
| Evidence | `logs/` from `python -m sim replay --scenario public --seed 11` |
| Other public seeds | 12, 13, 14, 15 |

Only files under `src/` are graded. The invariants are in `INVARIANTS.md`;
check a replay with `python -m sim check --trace <out>/trace.jsonl`.

---

Tenant D says jobs they cancelled ahead of time still ran and were billed. Their pipeline cancels
the part of the nightly batch it doesn't need before submitting the batch; each of those cancels
came back CANCELLED, and most nights that works. Last night router-1 restarted a couple of times
during the window, and a handful of the cancelled jobs ran to completion, and status said
SUCCEEDED. Logs from a replay of that window attached.
