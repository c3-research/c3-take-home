# Idempotency on C3

Status: CURRENT. Platform-wide rules for making requests safe to retry. The
network can drop, duplicate, delay and reorder any message (`runtime-v3.md`
section 2), so every state-changing request on C3 must be idempotent.

## 1. Terms

- **Idempotency key.** A caller-chosen identifier for one logical request. Every
  retry of that request carries the same key.
- **Scope.** The namespace in which a key is unique. For tenant-facing APIs the
  scope is `(tenant, key)`: two tenants may use the same key string.
- **Deduplication record.** What the receiver stores for a key: the request's
  essential parameters and the outcome it returned.
- **Lifetime.** How long the receiver keeps a deduplication record. A retry
  inside the lifetime is deduplicated; a retry outside it is not. Each service
  documents its own lifetime and how it is measured. The platform does not set
  one. (The GPU's `op_id`, by contrast, never expires; see `gpu-api-v3.md`.)

## 2. Caller rules

1. **Choose the key before the first attempt, and persist it** if the retry
   could happen after a crash. A key drawn from `rng` after a restart is a new
   request, not a retry.
2. **Reuse the key on every retry** after a timeout, an ambiguous error (such as
   the GPU's `UNAVAILABLE`), or a restart.
3. **Never reuse a key for different work.** A key identifies one request.
4. Retry with backoff and a bounded number of attempts. Unbounded, immediate
   retries amplify overload (a retry storm) without improving the odds.

## 3. Receiver rules

1. **Look up the key first.** If a record exists, return the recorded outcome
   and do nothing else. Do not re-run side effects.
2. **Record before replying.** Write the deduplication record, together with
   the state change it describes, durably before sending the reply. If the two
   need separate writes, write them so that a crash between them is recoverable
   (for example, record the intent first and finish it on restart), because the
   runtime has no multi-key transactions.
3. **Make check-and-record atomic with respect to concurrent handlers.** Two
   duplicates of one request can be handled concurrently on the same node. If
   there is an `await` between "key not found" and "record written", both copies
   can pass the check. Mark the key as in progress before any `await`, and make
   the second copy wait for, or return, the first copy's outcome.
4. **Reject a mismatched retry.** If a request arrives with a known key but
   different essential parameters, reject it with an error rather than returning
   the other request's outcome or applying it as new.
5. **Keep records for the documented lifetime,** measured on the receiver's own
   clock, and expire them only after it. If the lifetime is expressed relative to
   a caller-supplied timestamp, correct for clock offset and drift
   (`runtime-v3.md` section 3).

## 4. Idempotency across services

When a request's effect is itself a call to another service (for example, a
job's work being submitted to the GPU), the downstream call must be idempotent
too, with a key derived deterministically from the upstream key. Otherwise a
duplicated upstream request, deduplicated correctly at the first hop, can still
fan out twice if the first attempt crashed after calling downstream but before
recording its outcome.

A good pattern: derive the downstream key (such as the GPU `op_id`) from the
upstream key and persist it before the downstream call; on recovery, query the
downstream service (`status`) with that key before calling it again.

## 5. Operations that are not naturally idempotent

- **Counters and balances.** "Add 10" is not idempotent. "Apply charge K" with a
  deduplication record for K is.
- **Cancellation and compensation.** A cancel or refund must name the request it
  cancels, and must be deduplicated like any other request. If it can arrive
  before the request it cancels, the receiver must record it so that the later
  request is resolved as if the two had arrived in order.
- **Notifications.** A one-way `send` can be duplicated. Receivers must tolerate
  duplicate notifications.

## 6. Checklist

- Key chosen once, persisted, reused on every retry.
- Receiver checks, records and replies in that order, durably.
- No `await` between the duplicate check and marking the key in progress.
- Mismatched retries rejected.
- Downstream calls keyed deterministically from the upstream key.
- Records kept for the documented lifetime.
