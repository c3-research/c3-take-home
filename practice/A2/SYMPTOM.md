# A2: incident report

| | |
| --- | --- |
| Service | jobrouter: asynchronous GPU job router with leases and retries |
| Case | A2 (one reported problem) |
| Budget | 900 s wall clock, $6.00 model spend |
| Evidence | `logs/` from `python -m sim replay --scenario public --seed 12` |
| Other public seeds | 22, 28, 29, 33 |

Only files under `src/` are graded. The invariants are in `INVARIANTS.md`;
check a replay with `python -m sim check --trace <out>/trace.jsonl`.

---

Since the router started taking long GC pauses this week, the lease-safety check on our replays
keeps flagging workers that sent a `report` after the router's lease for that job had already run
out (A3). It's a few jobs per afternoon, different workers each time, usually just after a router
pause. Clients still get their results and nothing has double-run that we know of yet, but this is
the check that is supposed to stop two workers acting on one job. Logs from one of the affected
afternoons attached; the other listed seeds show it too.
