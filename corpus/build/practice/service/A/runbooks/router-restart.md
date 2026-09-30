# Runbook: router crash and restart

Status: CURRENT (jobrouter 3.2).

The router keeps every job in one disk record per job (`job:<tenant>/<job_id>`),
written before any reply or action that depends on it. A crash loses only
in-memory state, which recovery rebuilds on boot.

## What recovery does

On boot the router logs `router_start` with a recovery summary:

| Field | Meaning |
| --- | --- |
| `jobs` | Job records loaded |
| `queued` | Jobs in the dispatch queue (QUEUED, in submission order) |
| `leased` | LEASED jobs whose lease was re-armed (`lease_restored` per job) |
| `releasing` | Operations whose release was requested but not yet observed complete; a release task restarts for each |
| `reserved` | Units still reserved: every operation not yet marked freed, including the releasing ones |

1. **Leases.** The router cannot tell how much of a lease elapsed while it was
   down, so it treats every LEASED job's lease as granted at boot. The holder
   keeps its epoch and may keep renewing and report as usual. If it does not,
   the job is re-granted 2.504 s after boot (`LEASE_S * (1 + 2 * DRIFT_RATE) +
   REGRANT_GRACE_S`), with epoch + 1 and the same op_id.
2. **Capacity.** The ledger is recomputed from the operations recorded in each
   job. Units of an operation whose release was requested before the crash stay
   reserved until ReleaseComplete is observed again: the release is re-issued
   (`release` is idempotent) and the router waits for `release_complete` or
   `status.released == true`.
3. **Workers.** Worker incarnations are re-learned from `hello` and `acquire`.
   Acquire replies cached before the crash are gone; a worker whose acquire
   reply was lost simply acquires again, and the orphaned grant expires.

`recovery_audit` lines report inconsistent records (none are expected). The
router does not repair them automatically.

## Checks after a restart

- `router_start` shows the expected `jobs` count.
- `reserved` returns to the level implied by running jobs within a few seconds
  (`capacity_freed` lines for the `releasing` operations).
- No job is stuck LEASED for more than a few seconds after boot (see
  `stuck-or-slow-jobs.md`).

## Do not

- Do not delete job records to "unstick" a job. The record is the job's
  deduplication record for its `(tenant, job_id)` key and holds its epoch; a
  recreated job would restart epochs at 1 and could run twice.
- Do not free capacity by hand for operations in `releasing`: the GPU still
  counts those units until ReleaseComplete.
