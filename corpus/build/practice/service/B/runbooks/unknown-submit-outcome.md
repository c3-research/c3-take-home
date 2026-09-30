# Runbook: GPU operations that never appear

Status: CURRENT (service version 2.2). Complements `operational-notes.md`
section 4 and `platform/gpu-api-v3.md` sections 3.2 and 4.

## 1. The rule after an ambiguous submit

A submit that times out, or returns `UNAVAILABLE`, is logged
`gpu_submit_unknown` and resolved with `status` on the same `op_id`:

- the operation exists: `gpu_submit_confirmed`, carry on with it;
- `NOT_FOUND`: wait `UNKNOWN_OUTCOME_BACKOFF_S` and **submit again with the
  same `op_id`**. The resubmission is safe whether or not the first request
  is still on its way, because the GPU never runs one `op_id` twice.

`NOT_FOUND` is not a promise that the operation is about to appear. The first
request may have been dropped, and then only a resubmission creates it.

## 2. What you see when it goes wrong

A map task or a reduce that logs `gpu_submit_unknown` and then only `status`
calls, with no `gpu_submit_confirmed` and no further `gpu_submit`, is waiting
for an operation that does not exist. Its batch is never reported and its job
stays in MAPPING or REDUCING.
