"""Pluggable codebase loader.

A *codebase root* is a directory with:

    src/                    service code (imported by this loader, under the determinism guard)
    scenarios/*.yaml        scenarios
    invariants.py           check(events) -> [Violation]    (or sim/invariants.py in a case)
    scenarios.py            optional scenario loader         (or sim/scenarios.py in a case)
    case.json               optional; public_seeds used by `seeds` and as the default seed

The optional scenario loader module may define:

    build(sim, scenario)         add nodes (after any scenario `nodes:` entries are added)
    ACTIONS = {name: async fn(client, args) -> outcome dict}
    EXPECT  = {key: fn(value, events) -> [Violation]}   extra `expect:` keys
    DEFAULT_TARGET = "router-1"  default destination for client actions
    SCENARIO_KEYS = {...}        extra top-level scenario keys it understands
    transform(scenario) -> scenario   applied after loading, before the run

Service modules are (re)imported by this loader under the determinism guard.
`fresh=True` purges them from sys.modules first, so module-level state cannot
leak between runs in one process.
"""

import importlib
import importlib.util
import json
import os
import sys

from .determinism import GUARD, STATE
from .errors import ConfigError, NondeterminismError

_LOADER_NAMES = ("sim/scenarios.py", "scenarios.py")
_INVARIANT_NAMES = ("sim/invariants.py", "invariants.py")


def _find(root, names):
    for n in names:
        p = os.path.join(root, n)
        if os.path.isfile(p):
            return p
    return None


def _module_names(src):
    """Dotted names of every module under src (packages first, sorted)."""
    names = []

    def walk(d, prefix):
        for entry in sorted(os.listdir(d)):
            p = os.path.join(d, entry)
            if entry.startswith((".", "_")) and entry != "__init__.py":
                continue
            if os.path.isdir(p):
                if entry in ("tests", "test") or not entry.isidentifier():
                    continue
                if os.path.isfile(os.path.join(p, "__init__.py")):
                    names.append(prefix + entry)
                    walk(p, prefix + entry + ".")
            elif entry.endswith(".py") and entry != "__init__.py":
                stem = entry[:-3]
                if stem.isidentifier() and not stem.startswith("test_"):
                    names.append(prefix + stem)
    if os.path.isdir(src):
        walk(src, "")
    return names


def _load_path(modname, path):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(modname, None)
        raise
    return mod


def _under(path, d):
    try:
        return os.path.commonpath([os.path.realpath(path), d]) == d
    except ValueError:
        return False


class Codebase:
    def __init__(self, root, src, loader, invariants_path, case):
        self.root = root
        self.src = src
        self.loader = loader
        self.invariants_path = invariants_path
        self._invariants = None
        self.case = case

    # --- scenario loader hooks ---------------------------------------------------
    @property
    def actions(self):
        return dict(getattr(self.loader, "ACTIONS", {}) or {})

    @property
    def expect_checks(self):
        return dict(getattr(self.loader, "EXPECT", {}) or {})

    @property
    def default_target(self):
        return getattr(self.loader, "DEFAULT_TARGET", None)

    @property
    def scenario_keys(self):
        return set(getattr(self.loader, "SCENARIO_KEYS", ()) or ())

    def transform(self, sc):
        fn = getattr(self.loader, "transform", None)
        return fn(sc) if fn else sc

    def build(self, sim, sc):
        from .scenario import expand_nodes
        for name, cls_path, cfg in expand_nodes(sc):
            cls = resolve_class(cls_path)
            sim.add_node(name, cls, config=cfg)
        fn = getattr(self.loader, "build", None)
        if fn is not None:
            fn(sim, sc)

    # --- invariants ----------------------------------------------------------------
    @property
    def invariants(self):
        if self._invariants is None and self.invariants_path:
            d = os.path.dirname(self.invariants_path)
            if d not in sys.path:
                sys.path.append(d)
            self._invariants = _load_path("_c3sim_invariants", self.invariants_path)
        return self._invariants

    def check(self, events):
        inv = self.invariants
        if inv is None or not hasattr(inv, "check"):
            return []
        return [normalise_violation(v) for v in (inv.check(events) or [])]

    def public_seeds(self):
        return list((self.case or {}).get("public_seeds") or [])


def normalise_violation(v):
    if isinstance(v, dict):
        return {"invariant": str(v.get("invariant", "?")), "t": v.get("t"),
                "detail": str(v.get("detail", ""))}
    for attr in ("invariant", "t", "detail"):
        if not hasattr(v, attr):
            return {"invariant": "?", "t": None, "detail": str(v)}
    return {"invariant": str(v.invariant), "t": v.t, "detail": str(v.detail)}


def resolve_class(path):
    if ":" in path:
        mod, _, attr = path.partition(":")
    else:
        mod, _, attr = path.rpartition(".")
    if not mod or not attr:
        raise ConfigError(f"node class {path!r}: expected 'package.module:Class'")
    with GUARD:
        m = importlib.import_module(mod)
    try:
        obj = m
        for part in attr.split("."):
            obj = getattr(obj, part)
    except AttributeError:
        raise ConfigError(f"node class {path!r}: {attr} not found in {mod}") from None
    return obj


def find_root(start=None):
    """Codebase root: $C3SIM_ROOT, else the nearest directory (from cwd upward)
    containing scenarios/ and src/."""
    env = os.environ.get("C3SIM_ROOT")
    if env:
        return os.path.abspath(env)
    # Case layout (clarifications C1): <case>/sim/c3sim/ -> <case>
    pkg = os.path.dirname(os.path.abspath(__file__))
    case = os.path.dirname(os.path.dirname(pkg))
    if (os.path.basename(os.path.dirname(pkg)) == "sim"
            and (os.path.isdir(os.path.join(case, "src"))
                 or os.path.isdir(os.path.join(case, "scenarios")))):
        return case
    d = os.path.abspath(start or os.getcwd())
    while True:
        if os.path.isdir(os.path.join(d, "scenarios")) and os.path.isdir(os.path.join(d, "src")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.path.abspath(start or os.getcwd())
        d = parent


def load_codebase(root, fresh=True):
    """Import a codebase's service modules (guarded), scenario loader and case.json."""
    root = os.path.abspath(root)
    src = os.path.join(root, "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    real_src = os.path.realpath(src)
    loader_path = _find(root, _LOADER_NAMES)
    inv_path = _find(root, _INVARIANT_NAMES)
    if fresh:
        for name, mod in list(sys.modules.items()):
            f = getattr(mod, "__file__", None)
            if f and _under(f, real_src):
                del sys.modules[name]
        for name in ("_c3sim_scenarios", "_c3sim_invariants"):
            sys.modules.pop(name, None)
        importlib.invalidate_caches()
    STATE.violation = None
    try:
        with GUARD:
            for name in _module_names(src):
                importlib.import_module(name)
    except NondeterminismError:
        raise
    loader = None
    if loader_path:
        d = os.path.dirname(loader_path)
        if d not in sys.path:
            sys.path.append(d)
        loader = _load_path("_c3sim_scenarios", loader_path)
    case = None
    cj = os.path.join(root, "case.json")
    if os.path.isfile(cj):
        with open(cj, encoding="utf-8") as f:
            case = json.load(f)
    return Codebase(root, src, loader, inv_path, case)
