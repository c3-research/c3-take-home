# jobrouter: operational notes and deployment constants

Status: CURRENT (jobrouter 3.2). This is the authoritative list of the numbers
jobrouter is built and deployed against. In the code they all live in
`jobrouter/config.py` and are used by name. Times are seconds on the local
clock of the node that uses them.

## Deployment assumptions

| Assumption | Value |
| --- | --- |
| GPU capacity the service is deployed against | **12 units** (`DEFAULT_GPU_CAPACITY`; a deployment may override it with `service.gpu_capacity`) |
| Clock drift bound, any node | **1000 ppm** (`DRIFT_RATE` = 0.001) |
| Clock offset bound, any two nodes | **0.5 s** (`MAX_CLOCK_OFFSET_S`) |
| Routers | exactly one, `router-1` |
| Worker slots (concurrent leases per worker) | **2** (`WORKER_SLOTS`) |

jobrouter never compares a timestamp from one node with another node's clock,
so the offset bound does not enter any lease computation; it is why the
router's `expires_at` field is informational only.

## Leases

| Constant | Value | Meaning |
| --- | --- | --- |
| `LEASE_S` | **2.0 s** | Lease length on the router's clock, from when the router processes the acquire or renew |
| `DRIFT_RATE` | **1000 ppm** | Drift bound used in both the holder and grantor formulas |
| `MAX_DRIFT_S` | **0.25 s** | Holder safety margin ("max_drift"), subtracted from every holder-side expiry |
| holder budget | **1.748 s** | `LEASE_S * (1 - DRIFT_RATE) - MAX_DRIFT_S`, counted from the holder's local *send* time of the acknowledged acquire or renew |
| `REGRANT_GRACE_S` | **0.5 s** | Extra router wait before a re-grant |
| re-grant wait | **2.504 s** | `LEASE_S * (1 + 2 * DRIFT_RATE) + REGRANT_GRACE_S`, from the router's last grant or renew of the lease |
| `RENEW_INTERVAL_S` | **0.5 s** | Holder renew period |
| `RENEW_TIMEOUT_S` | **0.4 s** | Renew RPC timeout |
| `LEASE_SCAN_S` | **0.25 s** | How often the router looks for leases past their re-grant wait |

Fencing rule: an action under a lease must carry `(key, epoch, boot)`, and the
router accepts it only if all three match the job record and the job is LEASED.
Epochs are per job, start at 1, increase by exactly 1 per grant, and are
persisted in the job record before the grant is sent.

After a router restart every LEASED job's lease is re-armed at full length
from boot (so it is re-granted no earlier than 2.504 s after boot).

## Workers

| Constant | Value | Meaning |
| --- | --- | --- |
| `ACQUIRE_TIMEOUT_S` | 0.5 s | Acquire RPC timeout |
| `ACQUIRE_MAX_TRIES` | 3 | Attempts per acquire `req_id` |
| `ACQUIRE_IDLE_S` + `ACQUIRE_JITTER_S` | 0.5 s + up to 0.2 s | Idle pause when the router has no work |
| `HELLO_TIMEOUT_S` | 0.5 s | Registration RPC timeout |
| `HELLO_BACKOFF_MAX_S` | 2.0 s | Registration retry backoff cap |
| `REPORT_TIMEOUT_S` | 0.5 s | Report RPC timeout; report retries back off from 0.2 s up to `LEASE_S / 4` = 0.5 s |

## GPU

| Constant | Value | Meaning |
| --- | --- | --- |
| `GPU_RPC_TIMEOUT_S` | 1.0 s | Timeout of every GPU RPC |
| `GPU_POLL_S` | 0.3 s | Status poll period for a running operation |
| `GPU_BACKOFF_BASE_S` / `GPU_BACKOFF_MAX_S` | 0.2 s / 2.0 s | Exponential backoff (doubling, +0–25% jitter) for submit on `CAPACITY`, status probes and release retries |
| `RELEASE_POLL_S` | 0.5 s | Fallback `status` poll while waiting for ReleaseComplete |
| `MAX_GPU_ATTEMPTS` | 8 | Failed operations per job before the job is FAILED |

op_id format: `<tenant>/<job_id>/a<attempt>`. An op_id is never reused for
different work, and every lease on the same attempt uses the same op_id.

Unknown submit outcomes (`RpcTimeout`, `UNAVAILABLE`) are resolved with
`status(op_id)` before any resubmission, and a resubmission always uses the same
op_id (gpu-api-v3 section 4).

Units are reserved from the attempt's first grant until ReleaseComplete is
observed (`release_complete` message or `status.released == true`). The reply to
`release` never frees units.

## Dispatch and monitoring

| Constant | Value | Meaning |
| --- | --- | --- |
| `HOL_HOLD_S` | 10.0 s | Longest time capacity is held back for the oldest job that does not fit |
| `STATS_INTERVAL_S` | 10.0 s | Period of the `router_stats` log line |

## Idempotency

| Key | Scope | Lifetime |
| --- | --- | --- |
| Client job key | `(tenant, job_id)` | Lifetime of the deployment (job records are never deleted) |
| Acquire request | `(worker, req_id)` | The worker's most recent acquire only |
| GPU op_id | platform-wide | Never expires (gpu-api-v3) |

## Reading the logs

Log lines are `t=<local time> node=<name> event=<event> k=v ...`. `t` is the
node's local clock, so compare lines from different nodes with care. Useful
events:

| Event | Node | Meaning |
| --- | --- | --- |
| `router_start` | router | Boot; recovery summary (`jobs`, `leased`, `releasing`, `queued`, `reserved`), `lease_s`, `regrant_after` |
| `worker_start` | worker | Boot; `lease_budget`, `max_drift`, `drift_rate` |
| `job_accepted`, `submit_duplicate`, `submit_mismatch` | router | Client submits |
| `lease_granted`, `lease_renewed`, `lease_expired`, `lease_revoked`, `lease_restored`, `lease_fenced` | router | Lease lifecycle |
| `lease_acquired`, `lease_lost`, `lease_abandoned`, `lease_expired_local` | worker | Holder view of the lease |
| `gpu_submit`, `gpu_submitted`, `gpu_submit_unknown`, `gpu_status_probe` | worker | Submit and its resolution |
| `report_applied`, `job_terminal` | router | Outcomes |
| `capacity_reserved`, `release_requested`, `release_observed`, `capacity_freed` | router | Capacity and release |
| `dispatch_blocked` | router | Oldest job does not fit; capacity is being held for it |
| `router_stats` | router | Periodic gauges and counters |
