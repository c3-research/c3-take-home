# Runbook: tasks marked lost while they are still running

Status: CURRENT (service version 2.2). Complements `design-overview.md`
section 6 and the `heartbeat` and `assign` entries in `api-reference.md`.

`coord` marks a task lost (`task_finished state=lost outcome=missing_from_heartbeat`)
when a heartbeat from the task's worker does not list it, and queues the
shard again. That is only safe if the heartbeat was built after the worker
accepted the task. This runbook states how the two sides make sure of it.

## 1. Heartbeat numbers

- A worker numbers its heartbeats 1, 2, 3, ... and a heartbeat's `hb_seq` is
  fixed at the moment the heartbeat is built and sent. The counter moves on
  for every heartbeat sent, whether or not that heartbeat is ever answered:
  a heartbeat that times out keeps its number and the next one gets the next
  number. A number is never sent twice by one worker incarnation.
- The `running` list in a heartbeat is taken in the same step as its number.
- The `hb_seq` in an `assign` reply is the number of the **last heartbeat the
  worker has sent** when it accepts the batch, acknowledged or not. Every task
  in the batch was created after that heartbeat was built.
- `coord` records **that number, taken from the reply,** as the task's
  acknowledgement point and judges a task missing only from a heartbeat whose
  `hb_seq` is larger. The reply's number is used as it is: it is routinely
  ahead of the newest heartbeat `coord` has processed, because heartbeats sent
  during a short cut are lost and the one sent after it may still be on its
  way. `coord`'s own view of the worker says nothing about how far ahead it is.

Links are not FIFO (`platform/runtime-v3.md` section 2): an `assign` reply can
overtake a heartbeat the same worker sent earlier. Taking the acknowledgement
point from the reply is what makes the order of arrival irrelevant. With any
other reference point, a heartbeat that the reply overtook marks the fresh
tasks lost.

## 2. What you see when it goes wrong

| Log pattern | Meaning |
| --- | --- |
| `task_finished ... outcome=missing_from_heartbeat` followed by `shard_requeued failures=0` for a task whose worker logs `task_start` and later `task_done` | The task was running; the shard is mapped twice |
| `task_abort reason=heartbeat` on the worker shortly after `batch_received` | The worker is told to drop the task it just accepted |

Neither pattern is expected on a run without worker crashes, fencing or
report timeouts.
