"""`python -m sim ...` (in a case) / `python -m c3sim ... --root DIR` (anywhere).

    replay  --scenario NAME|PATH [--seed N] [--faults FILE] [--out DIR]
    check   --trace DIR/trace.jsonl [--reference-messages N]
    seeds   --scenario NAME [--count 50]
    sweep   --scenario NAME --seeds 1-1000 [--procs 6] [--faults FILE] [--out FILE]
    determinism --scenario NAME --seeds 1,2 [--runs 20] [--subprocesses 2]

Exit codes: replay 0 whenever the simulation ran (2 bad arguments/config,
3 runtime error such as NondeterminismError); check 0 if no violations, 1
otherwise; sweep/determinism 0 if every seed passed, 1 otherwise.
"""

import argparse
import json
import os
import sys
import time

from .errors import ConfigError, NondeterminismError


def _reexec_with_hashseed():
    if os.environ.get("PYTHONHASHSEED") == "0" or os.environ.get("C3SIM_NO_REEXEC"):
        return
    env = dict(os.environ, PYTHONHASHSEED="0")
    argv = [sys.executable] + list(getattr(sys, "orig_argv", sys.argv)[1:])
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(sys.executable, argv, env)
    except OSError as e:  # pragma: no cover
        print(f"c3sim: warning: could not re-exec with PYTHONHASHSEED=0: {e}", file=sys.stderr)


def _parser(prog):
    p = argparse.ArgumentParser(prog=prog, description="c3sim deterministic simulator")
    p.add_argument("--root", help="codebase/case root (default: auto-detected)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--root", dest="root_sub", help=argparse.SUPPRESS)

    r = sub.add_parser("replay", help="run a scenario and write trace, logs and summary")
    common(r)
    r.add_argument("--scenario", required=True)
    r.add_argument("--seed", type=int)
    r.add_argument("--faults")
    r.add_argument("--out")

    c = sub.add_parser("check", help="run the codebase's invariants and the expect block on a trace")
    common(c)
    c.add_argument("--trace", required=True)
    c.add_argument("--reference-messages", type=int)

    s = sub.add_parser("seeds", help="list public seeds")
    common(s)
    s.add_argument("--scenario", required=True)
    s.add_argument("--count", type=int, default=50)

    w = sub.add_parser("sweep", help="replay + check many seeds")
    common(w)
    w.add_argument("--scenario", required=True)
    w.add_argument("--seeds", required=True, help="e.g. 1-1000 or 3,5,9")
    w.add_argument("--faults")
    w.add_argument("--procs", type=int, default=1)
    w.add_argument("--reference-messages", type=int)
    w.add_argument("--out", help="write per-seed results (JSON) here")

    d = sub.add_parser("determinism", help="replay seeds repeatedly and compare trace hashes")
    common(d)
    d.add_argument("--scenario", required=True)
    d.add_argument("--seeds", required=True)
    d.add_argument("--runs", type=int, default=20)
    d.add_argument("--subprocesses", type=int, default=0)
    d.add_argument("--faults")
    return p


def main(argv=None, root=None, prog=None):
    _reexec_with_hashseed()
    if prog is None:
        prog = "python -m " + (os.path.basename(root and os.path.join(root, "sim")) if root else "c3sim")
    args = _parser(prog).parse_args(argv)
    from .loader import find_root
    root = os.path.abspath(args.root_sub or args.root or root or find_root())
    try:
        return _dispatch(args, root)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except NondeterminismError as e:
        print(f"runtime error: NondeterminismError: {e}", file=sys.stderr)
        return 3


def _dispatch(args, root):
    from . import runner
    if args.cmd == "replay":
        t0 = time.perf_counter()
        cb, res = runner.run(root, args.scenario, args.seed, args.faults, fresh=True)
        out = args.out or os.path.join("runs", f"{res.sim.name}-s{res.sim.seed}")
        s = res.write(out)
        wall = time.perf_counter() - t0
        print(json.dumps({"out": out, "scenario": s["scenario"], "seed": s["seed"],
                          "outcomes": s["outcomes"], "all_resolved": s["all_resolved"],
                          "messages_sent": s["messages_sent"], "events": len(res.events),
                          "trace_sha256": s["trace_sha256"], "wall_s": round(wall, 3),
                          "error": s["error"]}, sort_keys=True))
        if res.error is not None:
            print(f"runtime error: {s['error']}", file=sys.stderr)
            return 3
        return 0
    if args.cmd == "check":
        from .check import run_checks
        from .loader import load_codebase
        from .trace import read_trace
        cb = load_codebase(root, fresh=True)
        events = read_trace(args.trace)
        v = run_checks(events, cb, args.reference_messages)
        spath = os.path.join(os.path.dirname(os.path.abspath(args.trace)), "summary.json")
        summary = {}
        if os.path.isfile(spath):
            with open(spath, encoding="utf-8") as f:
                summary = json.load(f)
        summary["invariants"] = v
        with open(spath, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, sort_keys=True)
            f.write("\n")
        print(json.dumps({"violations": len(v), "invariants": v}, sort_keys=True, indent=1))
        return 1 if v else 0
    if args.cmd == "seeds":
        from .loader import load_codebase
        cb = load_codebase(root, fresh=False)
        for s in runner.public_seeds(cb, args.scenario, args.count):
            print(s)
        return 0
    if args.cmd == "sweep":
        t0 = time.perf_counter()
        seeds = runner.parse_seeds(args.seeds)
        res = runner.sweep(root, args.scenario, seeds, args.faults, args.procs,
                           args.reference_messages)
        failed = [r for r in res if not r["ok"]]
        wall = time.perf_counter() - t0
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(res, f, indent=1, sort_keys=True)
        print(json.dumps({"seeds": len(res), "failed": len(failed), "wall_s": round(wall, 2),
                          "failures": failed[:20]}, sort_keys=True, indent=1))
        return 1 if failed else 0
    if args.cmd == "determinism":
        ok = True
        for seed in runner.parse_seeds(args.seeds):
            r = runner.determinism(root, args.scenario, seed, args.runs, args.faults,
                                   args.subprocesses)
            ok &= r["ok"]
            print(json.dumps({"seed": seed, "ok": r["ok"], "distinct": sorted(set(r["hashes"]))}))
        return 0 if ok else 1
    raise ConfigError(f"unknown command {args.cmd}")
