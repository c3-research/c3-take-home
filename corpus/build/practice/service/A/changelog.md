# jobrouter changelog

## 3.2.0 (current)

- Dispatch: head-of-line reservation. The oldest queued job that does not fit
  holds its units back for up to `HOL_HOLD_S` = 10 s, so large jobs are no
  longer starved by a stream of small ones.
- Release tasks wake on the `release_complete` message instead of waiting for
  the next status poll; the 0.5 s status poll stays as the fallback.
- Deployment capacity raised from 8 to 12 GPU units.
- New `stats` admin call and a `router_stats` log line every 10 s.
- `router_start` and `worker_start` log the lease constants in effect.

## 3.1.0

- Worker incarnations: workers register with `hello {boot}`; a higher boot
  count revokes the leases of earlier incarnations at once instead of waiting
  for them to expire. Holder requests carry `boot` and are fenced on it.
- Acquire requests carry a `req_id`; a retried acquire gets the grant already
  made instead of a second lease.
- Cancel tombstones: a cancel for an unknown key is recorded, and a later
  submit of that key is acknowledged but never runs.

## 3.0.0

Rewritten for GPU API v3 and the lease rules in `platform/leases-and-fencing.md`.
Replaces the 2.x design described in `design-overview-v2.md`.

- Holders measure leases from their **send** time and subtract the drift budget:
  expiry at `sent_at + LEASE_S * (1 - DRIFT_RATE) - MAX_DRIFT_S`, with
  `DRIFT_RATE` = 1000 ppm and `MAX_DRIFT_S` = 0.25 s. (2.x measured from the
  reply's arrival with no drift budget.)
- `LEASE_S` reduced from 5.0 s to 2.0 s, renewals every 0.5 s.
- The router waits `LEASE_S * (1 + 2 * DRIFT_RATE) + REGRANT_GRACE_S` (2.504 s)
  after the last grant or renew before re-granting. (2.x re-granted as soon as
  the lease expired on its clock.)
- op_ids are derived from the job key and attempt (`<tenant>/<job_id>/a<n>`)
  and shared by every holder of the attempt. (2.x drew a fresh op_id per
  lease.)
- `UNAVAILABLE` and timeouts on submit are resolved with `status(op_id)` before
  resubmitting (gpu-api-v3 section 4).
- The router, not the worker, releases operations, and counts units free only
  after ReleaseComplete. (2.x freed units on the `release` reply.)
- Job epochs persisted in the job record before each grant.

## 2.4.0

- Client submits deduplicated on `(tenant, job_id)`.
- Router job table moved to one disk record per job.

## 2.0.0

- First lease-based version. See `design-overview-v2.md` (superseded).
