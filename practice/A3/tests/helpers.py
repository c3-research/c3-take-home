"""Small fakes and scenario builders shared by the tests."""

import copy
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeClock:
    def __init__(self, t=0.0):
        self.t = float(t)

    def now(self):
        return self.t

    def advance(self, d):
        self.t += d


class FakeDisk:
    def __init__(self):
        self.data = {}

    def put(self, k, v):
        import json
        self.data[k] = json.loads(json.dumps(v))

    def get(self, k, default=None):
        import json
        v = self.data.get(k)
        return default if v is None else json.loads(json.dumps(v))

    def delete(self, k):
        self.data.pop(k, None)

    def keys(self, prefix=""):
        return sorted(k for k in self.data if k.startswith(prefix))


class FakeNode:
    """Enough of a Node for the store, ledger and dispatcher."""

    def __init__(self, t=0.0):
        self.clock = FakeClock(t)
        self.disk = FakeDisk()
        self.logs = []
        self.name = "router-1"
        self.boot_count = 0

    def log(self, event, **fields):
        self.logs.append(dict(fields, event=event))

    def events(self, name):
        return [e for e in self.logs if e["event"] == name]


def scenario(workload, faults=None, end_at=60.0, quiesce_at=40.0, capacity=12, workers=3,
             expect=None, service=None):
    """A scenario dict on the standard node layout."""
    return {
        "name": "test",
        "seed": 1,
        "end_at": end_at,
        "quiesce_at": quiesce_at,
        "liveness_window": 20.0,
        "gpu": {"capacity": capacity},
        "nodes": [
            {"name": "router-1", "class": "jobrouter.router:Router"},
            {"name": "worker-{i}", "count": workers, "class": "jobrouter.worker:Worker"},
        ],
        "service": dict(service or {"worker_slots": 2}),
        "client": {"target": "router-1"},
        "client_retry": {"timeout": 1.0, "max_attempts": 8, "backoff": 0.5,
                         "retry_on": ["UNAVAILABLE"]},
        "faults": copy.deepcopy(faults or {}),
        "workload": copy.deepcopy(workload),
        "expect": dict(expect or {}),
    }


def run(sc, seed=1):
    """Run a scenario (dict or name). -> (events, violations, summary)."""
    from c3sim import runner
    cb, res = runner.run(ROOT, sc, seed=seed)
    v = runner.check_result(cb, res)
    return res.events, v, res.summary()


def logs(events, event, node=None):
    return [e for e in events if e["kind"] == "log" and e.get("event") == event
            and (node is None or e["node"] == node)]


def outcomes(summary):
    return {a["id"]: a for a in summary["actions"]}
