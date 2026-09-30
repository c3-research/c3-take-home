# Leases and Fencing on C3

Status: CURRENT. Platform-wide rules for services that grant exclusive
responsibility (for a job, a shard, a worker slot, leadership) for a limited
time. Read `runtime-v3.md` first: the rules below follow from its clock, pause
and network guarantees.

## 1. Terms

- **Lease.** A grant from a *grantor* to a *holder*: "you alone may act on
  resource R until time T". A lease is only a promise about time, so it is only
  as safe as the holder's and grantor's clocks.
- **Epoch** (also called fencing token or generation). An integer attached to
  each grant of R. Every new grant of R carries a strictly larger epoch than any
  earlier grant of R.
- **Fencing.** Rejecting any action on R that carries an epoch smaller than the
  largest epoch already seen for R.

## 2. Leases alone are not enough

A lease holder can lose its lease without knowing it:

- its clock runs slow relative to the grantor's, so it believes the lease lasts
  longer than the grantor does;
- it pauses after checking that the lease is valid and before acting
  (`runtime-v3.md` section 4);
- its action is delayed in the network and arrives after the lease expired and
  the grantor re-granted R.

No holder-side check closes the last two gaps. **Every lease-protected action
must also be fenced by whoever applies it.**

## 3. Holder rules

1. **Measure from your own send time.** Start the lease interval on your local
   clock at the moment you *sent* the acquire or renew request, not when the
   reply arrived. The grantor may have started the lease any time after your
   send.
2. **Subtract the drift budget.** Treat the lease as expired at
   `sent_at + duration * (1 - r) - margin`, where `r` is the deployment's drift
   bound and `margin` covers the offset and pause allowances your service
   documents. The exact budget is part of each service's documented deployment
   assumptions.
3. **Re-check immediately before acting,** and carry the lease's epoch in the
   action so the receiver can fence it.
4. **Stop acting at expiry,** even if a renewal is in flight. Acting on a lease
   whose renewal has not been acknowledged is acting without a lease.
5. **After a restart, hold nothing.** A restarted node has lost its in-memory
   lease state. It must re-acquire, or reload a persisted lease and treat its
   elapsed time as unknown (so, expired).

## 4. Grantor rules

1. **Wait out the old lease before re-granting.** Re-grant R only after the old
   lease has expired *on the grantor's clock plus the drift and offset budget*,
   so that no honest holder can still believe it holds R.
2. **Issue strictly increasing epochs, and persist them before granting.** Write
   the new epoch to disk before replying to the new holder. A grantor that
   crashes and restarts must never issue an epoch it (or a predecessor) has
   already issued. Deriving epochs from `boot_count` alone is not sufficient if
   more than one node can grant R.
3. **Never lower an epoch,** including when processing delayed or duplicated
   messages.

## 5. Fencing rules (the resource side)

1. The node that applies a lease-protected change records, per resource, the
   highest epoch it has accepted, **durably** if the change is durable.
2. It rejects any request whose epoch is lower than the recorded one, and
   replies with an error that tells the sender it has been fenced.
3. A fenced holder must stop acting on R and must not retry the rejected action
   under its old epoch.
4. Checking the epoch and applying the change must happen without an `await` in
   between, or the check can go stale (another task may accept a higher epoch
   meanwhile).

### The GPU does not fence

The GPU service (`gpu-api-v3.md`) has no epoch parameter and accepts any
`submit` whose `op_id` is new. Services therefore enforce fencing **before**
work reaches the GPU:

- the node that decides to submit must hold a valid lease, and the node that
  owns the job's state must have accepted that lease's epoch;
- `op_id` identifies the logical operation. Two submissions that must not both
  run (for example, the same job attempt from an old and a new lease holder)
  must use the same `op_id`, so the GPU's idempotency catches what fencing
  missed. Two submissions that are genuinely different work must use different
  `op_id`s.
- when a holder is fenced or declared dead, the new holder must find out what
  the old holder already submitted (with `status`) before submitting again, and
  cancel or release the old holder's operations as the service's design
  requires.

## 6. Failure detection

Declaring a node dead is a lease decision in disguise: "if I have not heard from
you by T, your responsibilities expire". Heartbeat timeouts must budget for the
same clock drift, offset and pause allowances as leases (section 3), or healthy
nodes that pause briefly will be evicted. Once a node is declared dead, its
epoch must be advanced so that, if it was only paused, its late actions are
fenced.

## 7. Checklist

- Lease expiry computed from send time, minus drift and margin.
- Epoch carried on every protected action; checked and applied atomically.
- Epochs persisted before use; never reused after a restart.
- Grantor waits out the full old lease, with budget, before re-granting.
- Work sent to the GPU is fenced by the service, and deduplicated by `op_id`.
