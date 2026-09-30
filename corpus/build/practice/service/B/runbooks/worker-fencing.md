# Runbook: worker fencing, re-registration and restarts

Status: CURRENT (service version 2.2).

## 1. How fencing works

Every registration returns a **worker epoch**, strictly larger than any earlier
epoch of that worker and persisted on `coord` before the reply. `coord`
remembers only the current epoch of each worker and rejects commits and
reports carrying any other epoch with `FENCED`. Workers reject `assign` with a
different epoch (`STALE_EPOCH`). This is the fencing rule of
`platform/leases-and-fencing.md` section 5: the check and the state change
happen in one handler step, with no `await` between them.

An epoch stops being current when:

- `coord` declares the worker dead (`worker_dead reason=heartbeat_timeout`)
  after 5.0006 s of heartbeat silence: `WORKER_TIMEOUT_S = 3.0` s scaled by
  `1 + MAX_DRIFT` (200 ppm) plus `PAUSE_ALLOWANCE_S = 2.0` s;
- the worker registers again (after a restart, or after being told to
  `reregister`);
- `coord` restarts (membership is in memory; every worker's next heartbeat is
  answered `reregister`).

## 2. What a fenced worker does

On `{reregister: true}` the worker logs `worker_fenced`, aborts every task it
holds (cancelling their GPU operations) and registers again. A task that was
already past its GPU operation offers its commit under the old epoch and is
rejected; the worker reports it `fenced`. Nothing is resubmitted under the old
epoch.

## 3. Worker restarts

The new incarnation lists every `op/<op_id>` record its predecessor wrote
before submitting, and for each:

1. calls `status`; on `NOT_FOUND` waits `RECOVERY_SETTLE_S = 1.0` s and calls it
   once more, because the predecessor's submit may still be in the network;
2. if the operation exists: cancels it if it is not terminal, waits for a
   terminal state, releases it and waits for `ReleaseComplete`;
3. deletes the record (`recovery_op`).

It then registers with a new `boot`, which revokes everything the previous
incarnation held at `coord`.

## 4. Expected log pattern

```
coord     event=worker_dead worker=worker-2 epoch=7 reason=heartbeat_timeout ...
coord     event=worker_tasks_revoked worker=worker-2 tasks=3 reason=worker_dead
worker-2  event=worker_fenced epoch=6 tasks=3
worker-2  event=worker_registered epoch=8 ...
```

## 5. Healthy workers declared dead

If `worker_dead` appears for a worker that never crashed, compare the pause and
drift the node experienced with the budget in `operational-notes.md`. Raising
`WORKER_TIMEOUT_S` alone only delays the detection of real crashes; the pause
allowance is the documented knob for pause-heavy deployments.

## 6. Heartbeat sequence numbers belong to one epoch

`hb_seq` counts the heartbeats of one worker process. It starts again at 1
every time the process boots, and it carries on unchanged across a
`reregister` within the same boot. Nothing on `coord` may therefore assume that
a worker's `hb_seq` only grows. Heartbeat ordering is scoped to a
registration: every registration that is issued a new epoch starts a fresh
sequence on coord, and no sequence number seen under an earlier epoch is
compared with one sent under the new epoch. The "older than one already
processed" rule in `api-reference.md` applies within an epoch only, and so
does the "sent after the worker accepted it" test that reconciliation applies
to a task's acceptance number.

This is also why a registration revokes the worker's tasks at once rather than
leaving them for reconciliation to sort out. Tasks accepted under the old epoch
carry acceptance numbers from the old sequence, which the new sequence may not
reach for a long time.

This matters most for a worker process that crashes and comes back quickly.
It usually registers again well inside the dead-after budget, so `coord` still
lists it as alive when the new registration arrives, and its first heartbeats
carry sequence numbers far below the ones its predecessor reached.

Expected pattern for a fast restart:

```
coord     event=worker_registered worker=worker-3 epoch=4 boot=1 reg_seq=1
coord     event=worker_tasks_revoked worker=worker-3 tasks=2 reason=reregistered
coord     event=batch_assigned batch=worker-3/e4/b57 worker=worker-3 tasks=2 accepted=2 hb_seq=2
```

After that the worker stays registered under epoch 4 until it restarts again.
A worker that registers every few seconds (one `worker_registered` after
another, each with a new epoch) is being fenced over and over.
