# Runbook: worker registration and router outages

Status: CURRENT (jobrouter 3.2).

A worker registers each incarnation with `hello {boot}` before it acquires any
lease (`design-overview.md`, "Worker incarnations"). While the router is down or
unreachable, a worker that has just booted cannot register, and it retries.

## Registration retry policy

Every failed `hello` is followed by a backoff pause before the next attempt,
whatever the failure was: a timeout (`HELLO_TIMEOUT_S`, 0.5 s) and an error
reply are treated the same way. The pause is the service's standard
exponential backoff:

- first pause 0.2 s, doubling after each failed attempt;
- capped at 2.0 s (`HELLO_BACKOFF_MAX_S`);
- each pause stretched by a random 0–25% jitter drawn from the node's `rng`.

The cap is its own deployment constant, deliberately four times the hello
timeout; it is not tied to `HELLO_TIMEOUT_S` or to any other timeout. A worker
that has not registered cannot do any work, so it is tempting to retry
quickly, but every restarted worker retrying at the RPC-timeout pace sends
about one `hello` a second for as long as the outage lasts, more than twice
the documented rate. That is the retry storm the platform rules forbid
(`platform/idempotency.md`, caller rule 4), and it lands on the router the
moment it comes back.

## What to expect in the logs

Each failed attempt logs `worker_register_retry` with its `reason`. Once the
backoff has reached its cap, a restarted worker logs at most one every 2.5 s or
so (timeout plus pause), roughly a dozen in a 30 s outage, then one
`worker_registered` when the router answers. Twice that many or more per
worker means the backoff is not reaching its documented cap.
