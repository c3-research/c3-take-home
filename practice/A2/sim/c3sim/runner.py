"""High-level entry points: run a scenario on a codebase, sweeps, determinism checks."""

import json
import os
import subprocess
import sys

from .check import run_checks
from .errors import ConfigError
from .faults import stream
from .kernel import Sim
from .loader import load_codebase
from .scenario import load_scenario, resolve_scenario_path


def prepare(root, scenario, faults=None, fresh=True):
    """-> (codebase, scenario dict). `scenario` is a name, a path or a dict."""
    cb = load_codebase(root, fresh=fresh)
    if isinstance(scenario, dict):
        sc = load_scenario(scenario, faults, extra_keys=cb.scenario_keys)
    else:
        path = resolve_scenario_path(root, scenario)
        sc = load_scenario(path, faults, extra_keys=cb.scenario_keys)
    sc = cb.transform(sc)
    return cb, sc


def default_seed(cb, sc):
    if sc.get("seed") is not None:
        return int(sc["seed"])
    ps = cb.public_seeds()
    return int(ps[0]) if ps else 1


def build_sim(cb, sc, seed):
    sim = Sim(seed=seed, scenario=sc)
    sim.scenario["_actions"] = cb.actions
    sim.scenario["_default_target"] = cb.default_target
    cb.build(sim, sc)
    return sim


_warned = []


def _warn_hashseed():
    if os.environ.get("PYTHONHASHSEED") != "0" and not _warned:
        _warned.append(1)
        print("c3sim: warning: PYTHONHASHSEED is not 0; set iteration order in service code "
              "may differ between processes (the CLI re-executes itself with 0)", file=sys.stderr)


def run(root, scenario, seed=None, faults=None, fresh=True):
    """Load (fresh), build and run. Returns (codebase, RunResult)."""
    _warn_hashseed()
    cb, sc = prepare(root, scenario, faults, fresh=fresh)
    if seed is None:
        seed = default_seed(cb, sc)
    sim = build_sim(cb, sc, int(seed))
    return cb, sim.run()


def check_result(cb, result, reference_messages=None):
    return run_checks(result.events, cb, reference_messages)


def public_seeds(cb, scenario_name, count):
    seeds = [int(s) for s in cb.public_seeds()][:count]
    r = stream(0, "public-seeds", scenario_name)
    seen = set(seeds)
    while len(seeds) < count:
        s = r.randint(1, 2**31 - 1)
        if s not in seen:
            seen.add(s)
            seeds.append(s)
    return seeds


def parse_seeds(spec):
    """'1-100', '3,5,9', '1-10,20' -> list of ints."""
    out = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part[1:]:
            a, _, b = part.partition("-")
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


# --- sweeps ------------------------------------------------------------------------

def _sweep_one(args):
    root, scenario, faults, seed, reference = args
    try:
        cb, res = run(root, scenario, seed, faults, fresh=True)
    except Exception as e:  # noqa: BLE001
        return {"seed": seed, "ok": False, "error": f"{type(e).__name__}: {e}",
                "violations": [], "unresolved": 0}
    v = check_result(cb, res, reference)
    unresolved = sum(1 for a in res.actions if a["outcome"] == "unresolved")
    err = None if res.error is None else f"{type(res.error).__name__}: {res.error}"
    return {"seed": seed, "ok": not v and not unresolved and err is None,
            "violations": v[:10], "n_violations": len(v), "unresolved": unresolved,
            "error": err, "messages": sum(1 for e in res.events if e["kind"] == "msg_send"),
            "sha256": res.sha256()}


def sweep(root, scenario, seeds, faults=None, procs=1, reference=None):
    """Run many seeds; returns per-seed dicts. Uses fork-based worker processes."""
    jobs = [(root, scenario, faults, s, reference) for s in seeds]
    if procs <= 1 or len(jobs) <= 1:
        return [_sweep_one(j) for j in jobs]
    import multiprocessing as mp
    ctx = mp.get_context("fork")
    with ctx.Pool(procs) as pool:
        return list(pool.imap(_sweep_one, jobs, chunksize=max(1, len(jobs) // (procs * 8))))


# --- determinism ---------------------------------------------------------------------

def determinism(root, scenario, seed, runs=20, faults=None, subprocesses=0):
    """Replay `runs` times in-process (fresh imports) plus `subprocesses` times in
    fresh interpreters. Returns {"ok": bool, "hashes": [...]}."""
    hashes = []
    for _ in range(runs):
        _, res = run(root, scenario, seed, faults, fresh=True)
        hashes.append(res.sha256())
    env = dict(os.environ, PYTHONHASHSEED="0",
               PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
    pkg = __name__.rpartition(".")[0]
    code = (f"import sys, json; from {pkg}.runner import run; "
            f"a = json.loads(sys.argv[1]); _, r = run(*a); print(r.sha256())")
    for _ in range(subprocesses):
        arg = json.dumps([root, scenario, seed, faults, True])
        out = subprocess.run([sys.executable, "-c", code, arg], env=env, capture_output=True,
                             text=True, check=False)
        if out.returncode != 0:
            raise ConfigError(f"determinism subprocess failed: {out.stderr[-2000:]}")
        hashes.append(out.stdout.strip().splitlines()[-1])
    return {"ok": len(set(hashes)) == 1, "hashes": hashes, "seed": seed}
