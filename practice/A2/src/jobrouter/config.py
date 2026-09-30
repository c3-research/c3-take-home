"""Deployment constants for the job router.

Every number here is part of the service's documented deployment contract
(service docs: "Operational notes" and "Design overview"). Code refers to these
constants by name; nothing else in the package hard-codes a timeout, a lease
length or a clock budget.

Times are seconds on the local clock of the node that uses them.
"""

# --- Leases -------------------------------------------------------------------------

# Length of a job lease granted by the router, measured on the router's clock
# from the moment it processes the acquire or renew request.
LEASE_S = 2.0

# Clock drift bound for the deployment: no node's clock runs faster or slower
# than true time by more than this fraction (1000 ppm).
DRIFT_RATE = 1000e-6

# Holder-side safety margin ("max_drift"). A lease holder treats its lease as
# expired at  sent_at + LEASE_S * (1 - DRIFT_RATE) - MAX_DRIFT_S  on its own
# clock, where sent_at is when it sent the acquire/renew request.
MAX_DRIFT_S = 0.25

# Worst-case offset between the clocks of any two nodes. Timestamps issued by
# another node (such as the router's expires_at) are never compared with the
# local clock; this bound is why.
MAX_CLOCK_OFFSET_S = 0.5

# Extra time the router waits after a lease has expired on its own clock before
# it re-grants the job (on top of the drift allowance below).
REGRANT_GRACE_S = 0.5

# How often a holder renews its lease, and the renew RPC timeout.
RENEW_INTERVAL_S = 0.5
RENEW_TIMEOUT_S = 0.4

# How often the router scans for leases that have run out.
LEASE_SCAN_S = 0.25

# --- Dispatch -----------------------------------------------------------------------

# Longest time (router clock) the dispatcher holds capacity back for the oldest
# queued job that does not fit, before falling back to first-fit.
HOL_HOLD_S = 10.0

# Interval between router_stats log lines.
STATS_INTERVAL_S = 10.0


def regrant_after(lease_s=LEASE_S):
    """Router-clock delay after the last acknowledgement before a re-grant."""
    return lease_s * (1.0 + 2.0 * DRIFT_RATE) + REGRANT_GRACE_S


def holder_budget(lease_s=LEASE_S):
    """Holder-clock validity of a lease, counted from the request's send time."""
    return lease_s * (1.0 - DRIFT_RATE) - MAX_DRIFT_S


# --- Workers ------------------------------------------------------------------------

# Concurrent leases a worker holds (one per slot).
WORKER_SLOTS = 2

# Acquire RPC timeout; attempts per acquire request id; idle poll interval
# (plus up to ACQUIRE_JITTER_S of random jitter) when the router has no work.
ACQUIRE_TIMEOUT_S = 0.5
ACQUIRE_MAX_TRIES = 3
ACQUIRE_IDLE_S = 0.5
ACQUIRE_JITTER_S = 0.2

# Registration (hello) RPC timeout and retry cap.
HELLO_TIMEOUT_S = 0.5
HELLO_BACKOFF_MAX_S = 2.0

# Report RPC timeout.
REPORT_TIMEOUT_S = 0.5

# --- GPU ----------------------------------------------------------------------------

# Capacity the service is deployed against, in GPU units. Scenarios may state
# the deployment's capacity as service.gpu_capacity.
DEFAULT_GPU_CAPACITY = 12

# Timeout for every GPU RPC (submit, status, cancel, release).
GPU_RPC_TIMEOUT_S = 1.0

# Interval between status polls of a running operation.
GPU_POLL_S = 0.3

# Retry backoff for submit, status probes and release: exponential from BASE,
# doubling per attempt, capped at MAX.
GPU_BACKOFF_BASE_S = 0.2
GPU_BACKOFF_MAX_S = 2.0

# Interval between status checks while waiting for ReleaseComplete.
RELEASE_POLL_S = 0.5

# GPU operations (attempts) per job before the job is declared FAILED. Each
# attempt has its own op_id; a FAILED operation is never resubmitted.
MAX_GPU_ATTEMPTS = 8

# --- Names --------------------------------------------------------------------------

GPU_NODE = "gpu"
DEFAULT_ROUTER = "router-1"


def router_name(node_config):
    return str(node_config.get("router", DEFAULT_ROUTER))


def gpu_capacity(node_config):
    return int(node_config.get("gpu_capacity", DEFAULT_GPU_CAPACITY))


def worker_slots(node_config):
    return int(node_config.get("worker_slots", WORKER_SLOTS))
