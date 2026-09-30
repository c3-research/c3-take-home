"""Contract constants for shardmr.

Every number here is stated in the service documentation (service-docs/). The
code refers to these names; nothing else in the package hard-codes a timeout,
budget or limit.
"""

# --- Deployment assumptions ---------------------------------------------------

# Maximum clock drift between any two shardmr nodes, as a rate (200 ppm).
MAX_DRIFT = 200e-6

# Longest process pause a healthy node is expected to survive without being
# declared dead (seconds).
PAUSE_ALLOWANCE_S = 2.0

# --- Worker membership ----------------------------------------------------------

HEARTBEAT_INTERVAL_S = 0.5
HEARTBEAT_RPC_TIMEOUT_S = 0.4

# A worker is declared dead once no heartbeat has been received for
# WORKER_TIMEOUT_S * (1 + MAX_DRIFT) + PAUSE_ALLOWANCE_S on the coordinator clock.
WORKER_TIMEOUT_S = 3.0

LIVENESS_CHECK_INTERVAL_S = 0.25

REGISTER_RPC_TIMEOUT_S = 0.5
REGISTER_RETRY_S = 0.5


def worker_dead_after():
    """Silence (coordinator-local seconds) after which a worker is declared dead."""
    return WORKER_TIMEOUT_S * (1.0 + MAX_DRIFT) + PAUSE_ALLOWANCE_S


# --- Scheduling ---------------------------------------------------------------------

# Concurrent map tasks a worker may hold (tasks assigned and not yet reported).
WORKER_SLOTS = 3

# Largest number of tasks sent to one worker in a single `assign` batch.
MAX_BATCH_TASKS = 3

DISPATCH_INTERVAL_S = 0.1

ASSIGN_RPC_TIMEOUT_S = 0.5
ASSIGN_MAX_ATTEMPTS = 3

# A task is a straggler once it has been running longer than the shard's
# requested duration plus this slack (coordinator-local seconds since dispatch).
STRAGGLER_SLACK_S = 2.0

# Speculation starts only once this fraction of a job's shards has committed.
SPECULATION_MIN_COMMITTED_FRACTION = 0.5

# Speculative duplicates launched per shard per job attempt.
MAX_SPECULATIVE_PER_SHARD = 1

# GPU-level failures tolerated per shard per job attempt before the attempt is
# abandoned and a new attempt starts.
MAX_TASK_FAILURES = 2

# Job attempts before the job is marked FAILED.
MAX_JOB_ATTEMPTS = 3

# Failed reduce operations tolerated per job attempt before the job is FAILED.
MAX_REDUCE_FAILURES = 3

# --- GPU usage ------------------------------------------------------------------------

GPU_RPC_TIMEOUT_S = 1.0
GPU_POLL_INTERVAL_S = 0.25
CAPACITY_BACKOFF_INITIAL_S = 0.25
CAPACITY_BACKOFF_MAX_S = 2.0
UNKNOWN_OUTCOME_BACKOFF_S = 0.1
RELEASE_POLL_INTERVAL_S = 0.5

# After a worker restart, an operation that reports NOT_FOUND is re-checked once
# after this delay before its local record is dropped.
RECOVERY_SETTLE_S = 1.0

REDUCE_UNITS = 1
REDUCE_DURATION_S = 0.5

# --- Worker -> coordinator ---------------------------------------------------------------

COMMIT_RPC_TIMEOUT_S = 0.5
COMMIT_MAX_ATTEMPTS = 5
COMMIT_BACKOFF_S = 0.2

REPORT_RPC_TIMEOUT_S = 0.5
REPORT_MAX_ATTEMPTS = 4
REPORT_BACKOFF_S = 0.2

# --- Jobs ------------------------------------------------------------------------------

MAX_SHARDS_PER_JOB = 64
MAX_RECORDS_PER_SHARD = 4096
MAX_UNITS_PER_SHARD = 4
DEFAULT_MAP_DURATION_S = 1.0

# Name of the coordinator node.
COORDINATOR = "coord"
