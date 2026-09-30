#!/usr/bin/env python3
"""Build a public case from a clean codebase plus the incident's inject.patch.

    python grader/make_case.py C2 [--split hidden] [--public-seed 13] [--force]

Reads   private/<ID>/incident.yaml, inject.patch, scenarios/public.yaml (and
        optionally scenarios/faults/, signature.json, ticket.md, reference.patch)
        codebases/<X>/ (src, tests, invariants.py, scenarios/, README.md, ...)
        runtime/c3sim/ and the runtime's case sim/ entry point
Writes  cases/<split>/<ID>/  SYMPTOM.md logs/ src/ tests/ sim/ scenarios/ case.json README.md
        private/<ID>/refcounts.json (reference message counts for I3; skip with --no-refcounts)
        private/<ID>/regression-baseline.json (tests passing on the unfixed code; C16)

Sources for the public scenario, in order: private/<ID>/scenarios/public.yaml,
private/<ID>/public.yaml. Fault family: private/<ID>/scenarios/faults/, else the
codebase's scenarios/faults/. Ticket text: private/<ID>/ticket.md (the builder's
prose, appended under the generated header).

The public seed used for logs/ is --public-seed, else the first of
public_seeds whose replay matches signature.json, else the first public seed.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CASE_SPLITS, PRACTICE_CODEBASES, ROOT, GraderError, apply_patch, copy_tree,  # noqa: E402
                    load_yaml, rmtree, write_json)
import simcli  # noqa: E402
from signature import load_signature, match  # noqa: E402

SYMPTOM_HEADER = """\
# {id}: incident report

| | |
| --- | --- |
| Service | {service} |
| Case | {id} ({n_bugs_text}) |
| Budget | {budget_s:.0f} s wall clock, ${budget_usd:.2f} model spend |
| Evidence | `logs/` from `python -m sim replay --scenario public --seed {seed}` |
| Other public seeds | {other_seeds} |

Only files under `src/` are graded. The invariants are in `INVARIANTS.md`;
check a replay with `python -m sim check --trace <out>/trace.jsonl`.

---

"""

CODEBASE_EXTRA_FILES = ("README.md", "INVARIANTS.md", "scenario_schema.md")


def runtime_dir(runtime_root: Path | None) -> Path:
    r = Path(runtime_root) if runtime_root else ROOT / "runtime"
    if not (r / "c3sim").is_dir():
        raise GraderError(f"runtime not found: {r}/c3sim")
    return r


def build_sim(dest: Path, codebase: Path, runtime: Path) -> None:
    """case/sim/ per package `sim` with __main__.py, the runtime
    at sim/c3sim/, the codebase invariants at sim/invariants.py and its scenario
    loader at sim/scenarios.py when it has one."""
    sim = dest / "sim"
    tool = runtime / "tools" / "make_sim_dir.py"
    if tool.exists():   # B1's canonical assembler
        import importlib.util
        spec = importlib.util.spec_from_file_location("_make_sim_dir", tool)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.make_sim_dir(str(codebase), str(sim))
        if not (sim / "invariants.py").exists():
            raise GraderError(f"{codebase}: no invariants.py")
        return
    if sim.exists():
        rmtree(sim)
    sim.mkdir(parents=True)
    copy_tree(runtime / "c3sim", sim / "c3sim")
    entry = None
    for cand in (runtime / "case_sim", runtime / "sim", runtime / "c3sim" / "case_sim"):
        if (cand / "__main__.py").exists():
            entry = cand
            break
    if entry is None:
        raise GraderError(f"runtime has no case entry point (looked for {runtime}/case_sim/__main__.py)")
    for f in sorted(entry.iterdir()):
        if f.is_file() and f.suffix == ".py":
            (sim / f.name).write_bytes(f.read_bytes())
    if not (sim / "__init__.py").exists():
        (sim / "__init__.py").write_text("")
    inv = codebase / "invariants.py"
    if not inv.exists():
        raise GraderError(f"{codebase}/invariants.py missing")
    (sim / "invariants.py").write_bytes(inv.read_bytes())
    for name in ("scenarios.py", "scenario_loader.py"):
        if (codebase / name).exists():
            (sim / "scenarios.py").write_bytes((codebase / name).read_bytes())
            break


def default_split(case_id: str, codebase: str) -> str:
    return "practice" if codebase in PRACTICE_CODEBASES else "hidden"


def build_case(a) -> Path:
    priv_root = Path(a.private_root) if a.private_root else ROOT / "private"
    cb_root = Path(a.codebases_root) if a.codebases_root else ROOT / "codebases"
    cases_root = Path(a.cases_root) if a.cases_root else ROOT / "cases"
    priv = priv_root / a.id
    inc = load_yaml(priv / "incident.yaml")
    if not inc or inc.get("id") != a.id:
        raise GraderError(f"{priv}/incident.yaml missing or id != {a.id}")
    X = inc["codebase"]
    cb = cb_root / X
    if not (cb / "src").is_dir():
        raise GraderError(f"codebase {cb} has no src/")
    split = a.split or default_split(a.id, X)
    if split not in CASE_SPLITS:
        raise GraderError(f"split must be one of {CASE_SPLITS}")
    dest = cases_root / split / a.id
    if dest.exists():
        if not a.force:
            raise GraderError(f"{dest} exists (use --force to rebuild)")
        rmtree(dest)
    tmp = Path(tempfile.mkdtemp(prefix=f"mkcase-{a.id}-"))
    try:
        stage = tmp / a.id
        # src: clean + inject.patch
        copy_tree(cb / "src", stage / "src")
        if not (priv / "inject.patch").exists():
            raise GraderError(f"{priv}/inject.patch missing")
        apply_patch(stage, priv / "inject.patch")
        copy_tree(cb / "tests", stage / "tests")
        for name in CODEBASE_EXTRA_FILES:
            if (cb / name).exists():
                (stage / name).write_bytes((cb / name).read_bytes())
        # scenarios: codebase's clean scenarios, then the case's public scenario and fault family
        if (cb / "scenarios").is_dir():
            copy_tree(cb / "scenarios", stage / "scenarios")
        for bad in (stage / "scenarios").glob("bug-*.yaml"):
            bad.unlink()
        pub = next((p for p in (priv / "scenarios" / "public.yaml", priv / "public.yaml") if p.exists()), None)
        if pub is None:
            raise GraderError(f"{priv}/scenarios/public.yaml missing")
        (stage / "scenarios").mkdir(exist_ok=True)
        (stage / "scenarios" / "public.yaml").write_bytes(pub.read_bytes())
        if (priv / "scenarios" / "faults").is_dir():
            copy_tree(priv / "scenarios" / "faults", stage / "scenarios" / "faults")
        build_sim(stage, cb, runtime_dir(a.runtime_root))
        bugs = inc.get("bugs") or []
        seeds = [int(s) for s in (inc.get("public_seeds") or [])]
        if not seeds:
            raise GraderError("incident.yaml has no public_seeds")
        meta = {"id": a.id, "codebase": X, "budget_s": inc.get("budget_s", 900),
                "budget_usd": inc.get("budget_usd", 3.0 * max(1, len(bugs))), "public_seeds": seeds}
        write_json(stage / "case.json", meta)
        # logs from a public replay
        sig = load_signature(priv / "signature.json") if (priv / "signature.json").exists() else None
        chosen, report = None, []
        order = [a.public_seed] if a.public_seed is not None else seeds
        for s in order:
            out = tmp / "replays" / str(s)
            r = simcli.replay(stage, "public", s, out)
            if not r.ok:
                raise GraderError(f"public replay seed {s} failed on the buggy code: {r.error}")
            if sig is None or a.public_seed is not None:
                chosen = s
                if sig is not None:
                    ok, report = match(sig, simcli.load_trace(out / "trace.jsonl"))
                    if not ok:
                        print(f"warning: signature does not match on seed {s}:\n  " + "\n  ".join(report))
                break
            ok, report = match(sig, simcli.load_trace(out / "trace.jsonl"))
            if ok:
                chosen = s
                break
        if chosen is None:
            chosen = seeds[0]
            print(f"warning: signature.json matches no public seed; using seed {chosen}:\n  "
                  + "\n  ".join(report))
        copy_tree(tmp / "replays" / str(chosen) / "logs", stage / "logs")
        # SYMPTOM.md
        ticket = priv / "ticket.md"
        body = ticket.read_text() if ticket.exists() else "(ticket text missing: write private/<ID>/ticket.md)\n"
        if not ticket.exists():
            print(f"warning: {ticket} missing; SYMPTOM.md has a placeholder body")
        service = _service_name(stage / "README.md", X)
        n = len(bugs)
        kind = inc.get("type", "single")
        n_text = {"parallel": f"{n} independent issues, see the backlog below",
                  "shared-file-pair": "more than one issue may be involved"}.get(kind, "one reported problem")
        header = SYMPTOM_HEADER.format(id=a.id, service=service, n_bugs_text=n_text,
                                       budget_s=float(meta["budget_s"]), budget_usd=float(meta["budget_usd"]),
                                       seed=chosen, other_seeds=", ".join(str(s) for s in seeds if s != chosen) or "none")
        (stage / "SYMPTOM.md").write_text(header + body)
        dest.parent.mkdir(parents=True, exist_ok=True)
        copy_tree(stage, dest)
        print(f"built {dest} (public seed {chosen} for logs/)")
    finally:
        rmtree(tmp)
    if not a.no_refcounts and (priv / "reference.patch").exists() and (priv / "seeds.json").exists():
        write_refcounts(dest, priv, a.codebases_root)
    write_baseline(dest, priv, a.codebases_root)
    return dest


def write_baseline(case_dir: Path, priv: Path, codebases_root) -> None:
    """private/<ID>/regression-baseline.json: the tests/ and
    regression/ test ids that pass on the unfixed case code."""
    from evaluate import Case, compute_baseline
    case = Case(case_dir, priv, codebases_root)
    tmp = Path(tempfile.mkdtemp(prefix="baseline-"))
    try:
        b = compute_baseline(case, tmp)
        write_json(priv / "regression-baseline.json", b)
        print(f"wrote {priv / 'regression-baseline.json'} ({len(b['tests'])} tests, "
              f"{len(b['regression'])} regression tests gate; "
              f"{len(b['regression_not_passing'])} fail on unfixed code, "
              f"{len(b['tests_unmasked']) + len(b['regression_unmasked'])} dropped by partial-fix variants)")
    finally:
        rmtree(tmp)


def write_refcounts(case_dir: Path, priv: Path, codebases_root) -> None:
    """private/<ID>/refcounts.json: reference message counts per bug and hidden seed,
    so graders without reference.patch (the practice pack) can check I3."""
    from evaluate import Case, build_eval_tree
    from concurrent.futures import ThreadPoolExecutor
    case = Case(case_dir, priv, codebases_root)
    tmp = Path(tempfile.mkdtemp(prefix="refcounts-"))
    try:
        tree = tmp / "tree"
        build_eval_tree(case, case.dir / "src", tree)
        apply_patch(tree, priv / "reference.patch")
        out: dict[str, dict[str, int]] = {}
        for bug in case.bugs:
            seeds = case.bug_seeds(bug["id"]) + [int(s) for s in case.seeds.get("regenerated", [])
                                                  if isinstance(s, int)]
            scen = case.scenario_name(bug)

            def one(seed, _bid=bug["id"], _scen=scen):
                o = tmp / "runs" / _bid / str(seed)
                r = simcli.replay(tree, _scen, seed, o)
                if not r.ok:
                    raise GraderError(f"reference replay {_bid} seed {seed} failed: {r.error}")
                return str(seed), simcli.count_messages(o / "trace.jsonl")
            with ThreadPoolExecutor(4) as ex:
                out[bug["id"]] = dict(sorted(ex.map(one, sorted(set(seeds)))))
        write_json(priv / "refcounts.json", out)
        print(f"wrote {priv / 'refcounts.json'}")
    finally:
        rmtree(tmp)


def _service_name(readme: Path, X: str) -> str:
    if readme.exists():
        for line in readme.read_text().splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return f"codebase {X}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("id")
    ap.add_argument("--split", choices=CASE_SPLITS)
    ap.add_argument("--public-seed", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-refcounts", action="store_true")
    ap.add_argument("--private-root")
    ap.add_argument("--codebases-root")
    ap.add_argument("--cases-root")
    ap.add_argument("--runtime-root")
    a = ap.parse_args(argv)
    try:
        build_case(a)
    except GraderError as e:
        print(f"make_case: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
