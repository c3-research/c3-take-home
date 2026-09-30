# C3 Runtime Environment, version 3

Status: CURRENT. What a C3 service may and may not assume about the network,
clocks, crashes and disk. Every C3 service runs on this runtime, and every
service design note assumes these guarantees and nothing stronger.

## 1. Execution model

- A service is a set of **nodes**. Each node is one instance of the service
  class, with its own name (for example `router-1`, `worker-3`).
- Node code is written as `async def` coroutines that await only runtime
  primitives: `rpc`, `sleep`, and awaiting tasks created with `spawn`. Nodes run
  concurrently with each other and may run several of their own tasks
  concurrently. Between two `await` points a task runs without interruption, but
  any `await` can interleave with other tasks on the same node, including other
  handler invocations.
- Nodes share no memory. All communication is by message.
- Handler payloads and replies are JSON objects, copied on delivery.
- The only sources of time and randomness are the node's `clock` and `rng`.
  Wall-clock time, OS randomness, threads, subprocesses, sockets and `asyncio`
  are unavailable to service code.

## 2. Network

The network makes **no delivery guarantees**. Most messages arrive once and
promptly, but any message, including an RPC request or an RPC
reply, can be:

| Fault | What it means for callers |
| --- | --- |
| **Delayed** | Delivery takes a variable time. There is no upper bound that code may rely on. |
| **Dropped** | Never delivered. The sender is not told. |
| **Duplicated** | Delivered twice. A handler can run twice for one `rpc` or `send`. |
| **Reordered** | Two messages from the same sender to the same receiver can arrive in either order. There is no FIFO guarantee per link. |
| **Partitioned** | For a period, nodes in one group cannot reach nodes in another. Messages across the cut are dropped. Partitions heal. |

Consequences:

- **`RpcTimeout` means "unknown outcome".** The request may never have arrived,
  or it arrived and the handler ran (possibly to completion) but the reply was
  lost. Retrying a non-idempotent request after a timeout can apply it twice.
- A handler must be safe to run more than once for the same logical request.
  See `idempotency.md`.
- An old, delayed message can arrive after newer ones, including after the
  sender has crashed and restarted. Handlers that change state must be able to
  recognise and reject stale requests (see `leases-and-fencing.md`).
- A successful RPC reply proves the handler ran at least once. It does not
  prove the handler ran only once.
- `send` is one-way. Its delivery is never confirmed.

## 3. Clocks

- Each node has its own **local clock**, `clock.now()`. `sleep(s)` and RPC
  timeouts are measured on the local clock.
- There is **no synchronised clock**. A node's local clock runs at a rate that
  can differ slightly from true time (drift, in parts per million) and can be
  offset from other nodes' clocks by a fixed amount. Local clocks never run
  backwards.
- Timestamps from different nodes are **not comparable**. A timestamp taken on
  node X and compared with `clock.now()` on node Y is only meaningful after
  correcting for the worst-case offset and drift between them.
- The drift and offset bounds a service is designed for are part of that
  service's documented deployment assumptions (see its `service/<X>/` docs).
  Code that compares times must budget for them explicitly.

### Worst-case elapsed-time error

If a node measures an interval of length `d` on its local clock, the true
elapsed time lies in `[d / (1 + r), d / (1 - r)]` for drift bound `r` (for
example `r = 200e-6` at 200 ppm). If the interval starts from a timestamp issued
by another node, add the worst-case offset between the two clocks.

## 4. Process pauses

A node can **pause**: it freezes (as in a long garbage-collection pause or a VM
stall), runs none of its tasks, and then resumes where it left off. A paused
node is not crashed: it keeps its memory and resumes its tasks, but when it
resumes, its local clock has moved on by the length of the pause, and timers
that should have fired during the pause fire late.

Consequences:

- Code can be suspended **between any two awaits**, including between checking a
  condition (such as "my lease is still valid") and acting on it.
- Other nodes may declare a paused node dead, and hand its work to someone else,
  while it is paused. When it resumes it does not know this happened.

## 5. Crashes and restarts

- A crash stops a node instantly. All its tasks are dropped and never resumed,
  and all in-memory state is lost. RPCs in flight to the node time out at the
  caller; RPCs the node had in flight are abandoned.
- A crashed node restarts after a delay. The restarted node is a fresh
  instance: same name, same disk, `boot_count` incremented by one, and
  `on_start()` runs before it handles requests.
- Messages already in the network when the node crashed can still be delivered
  to the restarted instance.
- Anything that must survive a crash must be written to disk **before** the
  node acts on it or tells anyone about it.
- Crashes can happen at any await point, including between two disk writes.

## 6. Disk

- Each node has a private, durable key-value store. It survives crashes and
  restarts; it is not shared with other nodes.
- `put(k, v)` and `delete(k)` are durable when they return. Each single-key
  write is atomic.
- There are **no multi-key transactions**. A crash between two `put`s leaves the
  first written and the second not. Recovery code must handle every such
  intermediate state, or keep related data under one key.
- `keys(prefix)` lists keys. Code that depends on iteration order should sort.
- Values are JSON-serialisable.

## 7. Randomness

`rng` is a per-node random generator. It is the only permitted randomness.
After a restart the generator is reseeded, so a restarted node must not assume
it will draw the same values as before the crash. Identifiers that must stay
stable across restarts (operation ids, idempotency keys, epochs) must be
persisted, or derived from persisted data, not drawn afresh.

## 8. Logging

`log(event, **fields)` writes one structured line:
`t=<local time> node=<name> event=<event> k=v ...`. The `t` value is the node's
**local** clock, so log timestamps from different nodes are not directly
comparable (see section 3).

## 9. Summary of what you may not assume

- That a timeout means the request was not processed.
- That a message is delivered once, in order, or at all.
- That two nodes agree on the time.
- That code runs without pauses between a check and an action.
- That in-memory state survives, or that a restart is noticed by peers.
- That two disk writes happen together.
