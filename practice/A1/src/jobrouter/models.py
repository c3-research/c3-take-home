"""Job records, states and identifiers.

A job is identified by its idempotency key ``(tenant, job_id)``; the router
stores everything it knows about the job in one JSON record under one disk key,
so every state change is a single atomic write.

Record fields:

    key, tenant, job_id, units, duration, seq
    state        QUEUED | LEASED | SUCCEEDED | FAILED | CANCELLED
    attempt      1-based number of the current GPU attempt
    granted      whether any lease has been granted for the current attempt
    epoch        highest lease epoch issued for this job (0 = never leased)
    holder, holder_boot   worker (and its boot count) holding the current lease
    cancel       a cancel has been requested and not yet resolved
    ops          {op_id: {"attempt", "units", "rel", "outcome"}}: every GPU
                 operation this job may have created, and its release state
    result, final_op, last_report, precancelled
"""

from c3sim import RpcError

QUEUED = "QUEUED"
LEASED = "LEASED"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

JOB_TERMINAL = frozenset({SUCCEEDED, FAILED, CANCELLED})
OP_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})

# Release state of a GPU operation, from the router's point of view.
REL_HELD = "held"            # units reserved; operation may exist and is not released
REL_RELEASING = "releasing"  # release requested; waiting for ReleaseComplete
REL_FREED = "freed"          # ReleaseComplete observed; units are free

MODE_RUN = "run"
MODE_CANCEL = "cancel"


def job_key(tenant, job_id):
    return f"{tenant}/{job_id}"


def op_id_for(key, attempt):
    """Deterministic GPU op_id of a job attempt.

    Every lease on the same attempt uses the same op_id, so the GPU's
    idempotency deduplicates submissions from successive holders.
    """
    return f"{key}/a{int(attempt)}"


def current_op_id(job):
    return op_id_for(job["key"], job["attempt"])


def new_job(tenant, job_id, units, duration, seq):
    return {
        "key": job_key(tenant, job_id), "tenant": tenant, "job_id": job_id,
        "units": units, "duration": duration, "seq": seq,
        "state": QUEUED, "attempt": 1, "granted": False, "epoch": 0,
        "holder": None, "holder_boot": None, "cancel": False, "ops": {},
        "result": None, "final_op": None, "last_report": None, "precancelled": False,
    }


def tombstone(tenant, job_id, seq):
    """Record for a cancel that arrived before its job was submitted."""
    job = new_job(tenant, job_id, None, None, seq)
    job["state"] = CANCELLED
    job["precancelled"] = True
    return job


def is_terminal(job):
    return job["state"] in JOB_TERMINAL


def public_view(job):
    """What the router tells clients about a job."""
    view = {"tenant": job["tenant"], "job_id": job["job_id"], "state": job["state"],
            "attempt": job["attempt"], "cancel_requested": bool(job["cancel"])}
    if job["state"] == SUCCEEDED and job["result"] is not None:
        view["result"] = job["result"]
        view["op_id"] = job["final_op"]
    return view


# --- request validation ---------------------------------------------------------------

def _str_field(p, name):
    v = p.get(name)
    if not isinstance(v, str) or not v:
        raise RpcError("INVALID", f"{name} must be a non-empty string")
    return v


def parse_job_ref(p):
    """(tenant, job_id) from a client request."""
    if not isinstance(p, dict):
        raise RpcError("INVALID", "request must be an object")
    return _str_field(p, "tenant"), _str_field(p, "job_id")


def parse_submit(p):
    tenant, job_id = parse_job_ref(p)
    units = p.get("units")
    if not isinstance(units, int) or isinstance(units, bool) or units < 1:
        raise RpcError("INVALID", "units must be an integer >= 1")
    duration = p.get("duration", 1.0)
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
        raise RpcError("INVALID", "duration must be a number >= 0")
    return tenant, job_id, units, float(duration)


def parse_lease_ref(p):
    """(key, epoch, boot) from a holder request."""
    if not isinstance(p, dict):
        raise RpcError("INVALID", "request must be an object")
    key = _str_field(p, "key")
    epoch = p.get("epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise RpcError("INVALID", "epoch must be an integer >= 1")
    boot = p.get("boot", 0)
    if not isinstance(boot, int) or isinstance(boot, bool) or boot < 0:
        raise RpcError("INVALID", "boot must be an integer >= 0")
    return key, epoch, boot


def same_submission(job, units, duration):
    """Whether a repeated submit carries the same essential parameters."""
    return job["units"] == units and float(job["duration"]) == float(duration)
