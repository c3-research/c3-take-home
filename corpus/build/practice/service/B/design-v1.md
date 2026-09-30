> SUPERSEDED by design-overview.md. Kept for reference.

# shardmr design (version 1)

Status: SUPERSEDED. Describes shardmr 1.x. Do not use for current behaviour.

## Jobs

A job is identified by its `job_id` alone. A job has no attempts: a shard that
fails on the GPU is retried until it succeeds, up to 5 times, after which the
job is FAILED.

## Commits

Workers write shard outputs to the coordinator with `commit {job, shard,
output}`. The coordinator keeps the **latest** output it received for each
`(job, shard)`; a later commit replaces an earlier one. Because every task of a
shard computes the same output, the order did not matter in practice.

## Speculation

A task running longer than 1.5 x its shard's duration gets up to 2 speculative
duplicates. When one of them commits, the coordinator immediately sends
`abort` to the others.

## Failure detection

A worker is declared dead after 3.0 s without a heartbeat. Worker ids are the
node names; there are no epochs. A worker declared dead that comes back is
simply marked alive again on its next heartbeat.

## Reduce

The reduce reads the stored output of every shard of the job when the last
shard commits.

## GPU

Map operations use `op_id = <job_id>/<shard>/<try>`, where `try` counts retries
on the worker. A submit that returns `UNAVAILABLE` is retried with the next
`try`.
