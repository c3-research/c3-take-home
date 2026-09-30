"""Fault configuration (runtime.md section 4): parsing, validation, sampling."""

import fnmatch
import hashlib

from .determinism import OriginalRandom
from .errors import ConfigError

BUILTIN_NODES = ("client", "gpu")


def derive_seed(*parts):
    h = hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(h[:8], "big")


def stream(seed, *parts):
    return OriginalRandom(derive_seed(seed, *parts))


def glob_match(pattern, name):
    """Glob match. Built-in nodes (client, gpu) match only by exact name."""
    if pattern == name:
        return True
    if name in BUILTIN_NODES:
        return False
    return fnmatch.fnmatchcase(name, pattern)


def matching(pattern, names):
    return [n for n in names if glob_match(pattern, n)]


class Dist:
    """A delay/latency distribution. Accepts a number (constant) or a dict."""

    __slots__ = ("kind", "a", "b", "lo")

    def __init__(self, spec, what="distribution"):
        self.lo = 0.0
        if spec is None:
            spec = 0.0
        if isinstance(spec, (int, float)) and not isinstance(spec, bool):
            self.kind, self.a, self.b = "constant", float(spec), 0.0
            return
        if isinstance(spec, (list, tuple)) and len(spec) == 2:
            self.kind, self.a, self.b = "uniform", float(spec[0]), float(spec[1])
            return
        if not isinstance(spec, dict):
            raise ConfigError(f"{what}: expected number, [low, high] or {{dist: ...}}, got {spec!r}")
        d = dict(spec)
        kind = d.pop("dist", None)
        try:
            if kind == "exponential":
                self.kind, self.a, self.b = kind, float(d.pop("mean")), 0.0
                self.lo = float(d.pop("min", 0.0))
            elif kind == "uniform":
                self.kind, self.a, self.b = kind, float(d.pop("low")), float(d.pop("high"))
            elif kind in ("constant", "fixed"):
                self.kind, self.a, self.b = "constant", float(d.pop("value")), 0.0
            elif kind == "normal":
                self.kind, self.a, self.b = kind, float(d.pop("mean")), float(d.pop("std"))
                self.lo = float(d.pop("min", 0.0))
            else:
                raise ConfigError(f"{what}: unknown dist {kind!r} (exponential|uniform|constant|normal)")
        except KeyError as e:
            raise ConfigError(f"{what}: missing parameter {e}") from None
        if d:
            raise ConfigError(f"{what}: unknown keys {sorted(d)}")
        if self.a < 0 or self.b < 0 and self.kind != "normal":
            raise ConfigError(f"{what}: negative parameter")

    def sample(self, rng):
        k = self.kind
        if k == "constant":
            return self.a
        if k == "exponential":
            return self.lo + (rng.expovariate(1.0 / self.a) if self.a > 0 else 0.0)
        if k == "uniform":
            return rng.uniform(self.a, self.b)
        v = rng.gauss(self.a, self.b)
        return v if v > self.lo else self.lo


def _prob(d, key, what):
    v = d.pop(key, 0.0)
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{what}.{key}: expected a probability, got {v!r}") from None
    if not 0.0 <= v <= 1.0:
        raise ConfigError(f"{what}.{key}: probability must be in [0, 1], got {v}")
    return v


def _range_or_num(v, what, allow_none=False):
    if v is None and allow_none:
        return None
    if isinstance(v, (list, tuple)):
        if len(v) != 2:
            raise ConfigError(f"{what}: expected [low, high]")
        return (float(v[0]), float(v[1]))
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"{what}: expected number or [low, high], got {v!r}") from None


def sample_range(v, rng):
    if v is None:
        return None
    if isinstance(v, tuple):
        return rng.uniform(v[0], v[1])
    return v


class FaultConfig:
    """Parsed faults.yaml. All fields validated; unknown keys are errors."""

    def __init__(self, spec=None):
        spec = dict(spec or {})
        net = dict(spec.pop("network", None) or {})
        nodes = dict(spec.pop("nodes", None) or {})
        gpu = dict(spec.pop("gpu", None) or {})
        if spec:
            raise ConfigError(f"faults: unknown top-level keys {sorted(spec)} (network|nodes|gpu)")

        self.delay = Dist(net.pop("delay", 0.001), "faults.network.delay")
        self.drop = _prob(net, "drop", "faults.network")
        self.duplicate = _prob(net, "duplicate", "faults.network")
        self.reorder = _prob(net, "reorder", "faults.network")
        self.reorder_window = float(net.pop("reorder_window", 0.05))
        self.partitions = []
        for i, p in enumerate(net.pop("partitions", None) or []):
            p = dict(p)
            what = f"faults.network.partitions[{i}]"
            try:
                at, until, groups = float(p.pop("at")), float(p.pop("until")), p.pop("groups")
            except KeyError as e:
                raise ConfigError(f"{what}: missing {e}") from None
            if p:
                raise ConfigError(f"{what}: unknown keys {sorted(p)}")
            if not isinstance(groups, list) or not all(isinstance(g, list) for g in groups):
                raise ConfigError(f"{what}.groups: expected a list of lists of node globs")
            if until < at:
                raise ConfigError(f"{what}: until < at")
            self.partitions.append({"at": at, "until": until,
                                    "groups": [[str(x) for x in g] for g in groups]})
        if net:
            raise ConfigError(f"faults.network: unknown keys {sorted(net)}")

        self.crashes = []
        for i, c in enumerate(nodes.pop("crashes", None) or []):
            c = dict(c)
            what = f"faults.nodes.crashes[{i}]"
            node = c.pop("node", None)
            if not node:
                raise ConfigError(f"{what}: missing node")
            entry = {"node": str(node),
                     "at": _range_or_num(c.pop("at"), what + ".at") if "at" in c else None,
                     "rate_per_hour": float(c.pop("rate_per_hour", 0.0)),
                     "restart_after": _range_or_num(c.pop("restart_after", 1.0),
                                                    what + ".restart_after", allow_none=True)}
            if entry["at"] is None and entry["rate_per_hour"] <= 0:
                raise ConfigError(f"{what}: needs 'at' or 'rate_per_hour'")
            if c:
                raise ConfigError(f"{what}: unknown keys {sorted(c)}")
            if "gpu" == node or node == "client":
                raise ConfigError(f"{what}: built-in node {node!r} cannot crash")
            self.crashes.append(entry)

        self.clocks = []
        for i, c in enumerate(nodes.pop("clocks", None) or []):
            c = dict(c)
            what = f"faults.nodes.clocks[{i}]"
            node = c.pop("node", None)
            if not node:
                raise ConfigError(f"{what}: missing node")
            entry = {"node": str(node),
                     "drift_ppm": _range_or_num(c.pop("drift_ppm", 0.0), what + ".drift_ppm"),
                     "offset": _range_or_num(c.pop("offset", 0.0), what + ".offset")}
            if c:
                raise ConfigError(f"{what}: unknown keys {sorted(c)}")
            self.clocks.append(entry)

        self.pauses = []
        for i, c in enumerate(nodes.pop("pauses", None) or []):
            c = dict(c)
            what = f"faults.nodes.pauses[{i}]"
            node = c.pop("node", None)
            if not node:
                raise ConfigError(f"{what}: missing node")
            if "duration" not in c:
                raise ConfigError(f"{what}: missing duration")
            entry = {"node": str(node),
                     "at": _range_or_num(c.pop("at"), what + ".at") if "at" in c else None,
                     "rate_per_hour": float(c.pop("rate_per_hour", 0.0)),
                     "duration": _range_or_num(c.pop("duration"), what + ".duration")}
            if entry["at"] is None and entry["rate_per_hour"] <= 0:
                raise ConfigError(f"{what}: needs 'at' or 'rate_per_hour'")
            if c:
                raise ConfigError(f"{what}: unknown keys {sorted(c)}")
            if node == "client":
                raise ConfigError(f"{what}: the client cannot pause")
            self.pauses.append(entry)
        if nodes:
            raise ConfigError(f"faults.nodes: unknown keys {sorted(nodes)}")

        self.gpu_fail = _prob(gpu, "fail", "faults.gpu")
        self.gpu_lost_reply = _prob(gpu, "lost_reply", "faults.gpu")
        self.gpu_arf = _prob(gpu, "accepted_reported_failed", "faults.gpu")
        self.gpu_latency = Dist(gpu.pop("latency", 0.0), "faults.gpu.latency")
        if gpu:
            raise ConfigError(f"faults.gpu: unknown keys {sorted(gpu)}")

    def describe(self):
        return {
            "network": {"drop": self.drop, "duplicate": self.duplicate,
                        "reorder": self.reorder, "partitions": len(self.partitions)},
            "crashes": len(self.crashes), "pauses": len(self.pauses),
            "clocks": len(self.clocks),
            "gpu": {"fail": self.gpu_fail, "lost_reply": self.gpu_lost_reply,
                    "accepted_reported_failed": self.gpu_arf},
        }
