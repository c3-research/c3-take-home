"""Identifiers and durable key layout.

Identifiers are derived from persisted data only (job key, attempt number,
shard index, coordinator generation), so a restarted node recomputes the same
names and never reuses one for different work.
"""

JOB_PREFIX = "job/"
COMMIT_PREFIX = "commit/"
WORKER_PREFIX = "worker/"
GEN_KEY = "coord/gen"
OP_PREFIX = "op/"


def job_key(tenant, job_id):
    """Idempotency scope of a job: (tenant, job_id)."""
    return f"{tenant}:{job_id}"


def job_record_key(jkey):
    return JOB_PREFIX + jkey


def commit_key(jkey, attempt, shard):
    return f"{COMMIT_PREFIX}{jkey}/a{int(attempt)}/s{int(shard):04d}"


def commit_prefix(jkey, attempt):
    return f"{COMMIT_PREFIX}{jkey}/a{int(attempt)}/"


def worker_record_key(worker):
    return WORKER_PREFIX + worker


def task_id(jkey, attempt, shard, gen, n):
    """Map task name: unique per coordinator generation and dispatch counter."""
    return f"{jkey}/a{int(attempt)}/s{int(shard)}/g{int(gen)}.{int(n)}"


def map_op_id(tid):
    """GPU op_id of a map task. One task runs as exactly one GPU operation."""
    return "map/" + tid


def reduce_op_id(jkey, attempt, try_no):
    return f"reduce/{jkey}/a{int(attempt)}/r{int(try_no)}"


def worker_op_key(op_id):
    return OP_PREFIX + op_id


def batch_id(worker, epoch, n):
    return f"{worker}/e{int(epoch)}/b{int(n)}"
