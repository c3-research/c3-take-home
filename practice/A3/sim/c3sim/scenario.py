"""Scenario files: workload + faults (+ gpu, nodes, expect).

```yaml
name: base                     # default: file stem
seed: 1                        # default seed for replay
end_at: 120.0                  # run stops here (virtual seconds)
quiesce_at: 90.0               # random/probabilistic faults stop here (default end_at)
liveness_window: 30.0          # I2 window after quiesce_at
gpu: {capacity: 16, release_delay: {dist: uniform, low: 0.05, high: 0.3}}   # enables node "gpu"
nodes:                         # optional: nodes built without a scenario loader
  - {name: router-1, class: "pkg.router:Router", config: {...}}
  - {name: "worker-{i}", count: 3, class: "pkg.worker:Worker"}   # worker-1..worker-3
service: {...}                 # codebase config; merged under each node's config
client: {target: router-1}     # default destination of client actions
client_retry: {timeout: 2.0, max_attempts: 3, backoff: 0.5, retry_on: [UNAVAILABLE]}
faults: {network: ..., nodes: ..., gpu: ...}   # or a path to a faults YAML
workload: [...]                # see client.py
expect: {...}                  # extra assertions, see check.py
```
"""

import copy
import json
import os

from .errors import ConfigError

KNOWN_KEYS = frozenset({
    "name", "seed", "end_at", "quiesce_at", "liveness_window", "gpu", "nodes",
    "service", "client", "client_retry", "faults", "workload", "expect",
    "description", "meta",
})


def _load_file(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".json"):
        return json.loads(text)
    try:
        import yaml
    except ImportError:  # pragma: no cover
        try:
            return json.loads(text)
        except ValueError:
            raise ConfigError("PyYAML is required for YAML scenarios "
                              "(python3 -m pip install --user pyyaml)") from None
    data = yaml.safe_load(text)
    return {} if data is None else data


def load_faults(path):
    data = _load_file(path)
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: a faults file must be a mapping")
    if set(data) == {"faults"}:
        data = data["faults"] or {}
    return data


def resolve_scenario_path(root, scenario):
    """--scenario NAME -> <root>/scenarios/NAME.yaml; or a path to a file."""
    if os.path.isfile(scenario):
        return os.path.abspath(scenario)
    for cand in (os.path.join(root, "scenarios", scenario + ".yaml"),
                 os.path.join(root, "scenarios", scenario + ".yml"),
                 os.path.join(root, "scenarios", scenario + ".json"),
                 os.path.join(root, "scenarios", scenario)):
        if os.path.isfile(cand):
            return cand
    raise ConfigError(f"scenario {scenario!r} not found (looked for {root}/scenarios/{scenario}.yaml)")


def load_scenario(source, faults_override=None, extra_keys=()):
    """Load and normalise a scenario (path or dict). `faults_override` (path or
    dict) replaces the scenario's faults block entirely."""
    base_dir = os.getcwd()
    if isinstance(source, dict):
        sc = copy.deepcopy(source)
    else:
        sc = _load_file(source)
        base_dir = os.path.dirname(os.path.abspath(source))
        if not isinstance(sc, dict):
            raise ConfigError(f"{source}: a scenario must be a mapping")
        sc.setdefault("name", os.path.splitext(os.path.basename(source))[0])
    unknown = set(sc) - KNOWN_KEYS - set(extra_keys)
    if unknown:
        raise ConfigError(f"scenario: unknown keys {sorted(unknown)} (known: {sorted(KNOWN_KEYS)}; "
                          f"put codebase settings under 'service:')")
    if faults_override is not None:
        sc["faults"] = (load_faults(faults_override) if isinstance(faults_override, str)
                        else copy.deepcopy(faults_override))
    elif isinstance(sc.get("faults"), str):
        p = sc["faults"]
        if not os.path.isabs(p):
            p = os.path.join(base_dir, p)
        sc["faults"] = load_faults(p)
    sc.setdefault("faults", {})
    sc.setdefault("workload", [])
    for k in ("end_at", "quiesce_at", "liveness_window"):
        if sc.get(k) is not None:
            try:
                sc[k] = float(sc[k])
            except (TypeError, ValueError):
                raise ConfigError(f"scenario.{k}: expected a number") from None
    if sc.get("nodes") is not None and not isinstance(sc["nodes"], list):
        raise ConfigError("scenario.nodes: expected a list")
    return sc


def expand_nodes(sc):
    """scenario.nodes -> [(name, "module:Class", config)]."""
    out = []
    shared = sc.get("service") or {}
    for i, n in enumerate(sc.get("nodes") or []):
        if not isinstance(n, dict) or "name" not in n or "class" not in n:
            raise ConfigError(f"scenario.nodes[{i}]: needs name and class")
        extra = set(n) - {"name", "class", "count", "config"}
        if extra:
            raise ConfigError(f"scenario.nodes[{i}]: unknown keys {sorted(extra)}")
        count = n.get("count")
        cfg = dict(shared)
        cfg.update(n.get("config") or {})
        if count is None:
            out.append((str(n["name"]), str(n["class"]), cfg))
        else:
            for k in range(1, int(count) + 1):
                out.append((str(n["name"]).replace("{i}", str(k)), str(n["class"]), dict(cfg)))
    return out
