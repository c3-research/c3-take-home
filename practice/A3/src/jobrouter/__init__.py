"""jobrouter: an asynchronous GPU job router with leases, fencing and retries.

Nodes:
    router-1   `jobrouter.router:Router`  job table, leases, capacity (durable)
    worker-N   `jobrouter.worker:Worker`  runs leased jobs on the platform GPU
    gpu        platform GPU service (gpu-api-v3)

See README.md and the service docs for the protocol.
"""

__version__ = "3.2.0"
