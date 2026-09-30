#!/usr/bin/env python3
"""Score one candidate src/ against one case. Runs in its own process, after the
fixer is gone (grader.md step 3).

    python grader/evaluate.py --case-dir cases/practice/A1 --src /tmp/ws/src \
        --out /tmp/eval.json [--private-dir private/A1] [--workers 2]

Steps: build a clean evaluation tree (pristine sim/, tests/, scenarios/ from the
public case, private regression/ and bug scenarios, candidate src/ only), run the
public tests and the private regression tests, then for each bug replay its
scenario on each hidden seed twice, compare traces byte for byte, run the
invariant checker, the goodput check (I3) against the reference message count,
and the bug's `expect` block.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (GRADER, GraderError, apply_patch, copy_tree, diff_trees, find_private_dir,  # noqa: E402
                    find_regression_dir, grader_python, load_json, load_yaml, rmtree, sha256_text,
                    split_patch, tree_files, write_json)
import simcli  # noqa: E402

GOODPUT_FACTOR = 3.0
TEST_TIMEOUT_S = float(os.environ.get("C3_TEST_TIMEOUT_S", 300))
# Top-level names a patch may not add under src/: they would shadow the runtime,
# the test tooling or Python start-up hooks.
FORBIDDEN_TOP = {"sim", "c3sim", "pytest", "_pytest", "yaml", "conftest.py", "sitecustomize.py",
                 "usercustomize.py", "sim.py", "c3sim.py", "pytest.py", "yaml.py", "conftest",
                 "sitecustomize", "usercustomize"}


# ---------------------------------------------------------------- case material

class Case:
    def __init__(self, case_dir: Path, private_dir: Path | None = None,
                 codebases_root: Path | None = None):
        self.dir = Path(case_dir).resolve()
        self.meta = load_json(self.dir / "case.json")
        self.id = self.meta["id"]
        self.codebase = self.meta.get("codebase")
        self.private = Path(private_dir).resolve() if private_dir else find_private_dir(self.id, None, self.dir)
        self.incident = load_yaml(self.private / "incident.yaml") or {}
        if not self.codebase:
            self.codebase = self.incident.get("codebase")
        self.bugs = list(self.incident.get("bugs") or [])
        if not self.bugs:
            raise GraderError(f"{self.id}: incident.yaml lists no bugs")
        seeds_path = self.private / "seeds.json"
        self.seeds = load_json(seeds_path) if seeds_path.exists() else {}
        self.regression = find_regression_dir(self.private, self.codebase, codebases_root)
        self.budget_s = float(self.meta.get("budget_s", self.incident.get("budget_s", 900)))
        self.budget_usd = float(self.meta.get("budget_usd", self.incident.get("budget_usd", 3.0 * len(self.bugs))))

    def bug_seeds(self, bug_id: str) -> list[int]:
        s = self.seeds.get(bug_id)
        if not s:
            raise GraderError(f"{self.id}: seeds.json has no seeds for {bug_id}")
        return [int(x) for x in s]

    def scenario_name(self, bug: dict) -> str:
        scen = bug.get("scenario") or f"scenarios/{bug['id']}.yaml"
        return Path(scen).stem

    def reference_patch(self) -> Path | None:
        p = self.private / "reference.patch"
        return p if p.exists() else None


def build_eval_tree(case: Case, src_dir: Path, dest: Path) -> dict:
    """Pristine case (minus src/ and logs/) + candidate src/ + private material."""
    dest.mkdir(parents=True, exist_ok=True)
    for item in sorted(case.dir.iterdir()):
        if item.name in ("src", "logs") or item.name.startswith("."):
            continue
        if item.is_dir():
            copy_tree(item, dest / item.name)
        elif item.is_file():
            (dest / item.name).write_bytes(item.read_bytes())
    dropped = copy_tree(src_dir, dest / "src")
    removed = []
    for child in sorted((dest / "src").iterdir()) if (dest / "src").exists() else []:
        if child.name in FORBIDDEN_TOP:
            removed.append(f"src/{child.name}")
            rmtree(child) if child.is_dir() else child.unlink()
    if case.regression:
        copy_tree(case.regression, dest / "regression")
    priv_scen = case.private / "scenarios"
    if priv_scen.is_dir():
        copy_tree(priv_scen, dest / "scenarios")
    return {"dropped_symlinks": [f"src/{d}" for d in dropped], "dropped_files": removed}


# ---------------------------------------------------------------- tests

def pytest_results(tree: Path, which: str, log: Path) -> tuple[dict[str, str] | None, str]:
    """Run one suite in its own pytest process. Returns ({test id: outcome}, message).

    Outcomes: passed | failed | error | skipped. Collection errors appear as an
    `error` entry for the module. None means the run itself broke (timeout,
    no report); every test then counts as not passed."""
    d = tree / which
    if not d.is_dir() or not any(d.rglob("test*.py")):
        return {}, f"no {which}/ tests"
    env = simcli.clean_env(tree, with_src=True)
    t0 = time.time()
    xml = log.with_suffix(".xml")
    if xml.exists():
        xml.unlink()
    try:
        r = subprocess.run([grader_python(), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                            "--rootdir", str(tree), f"--junitxml={xml}", "-o", "junit_family=xunit2", which],
                           cwd=tree, env=env, capture_output=True, text=True, timeout=TEST_TIMEOUT_S)
        out, code = r.stdout + r.stderr, r.returncode
    except subprocess.TimeoutExpired as e:
        out, code = f"TIMEOUT after {TEST_TIMEOUT_S}s\n{e.stdout or ''}", -1
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(out)
    tail = out.strip().splitlines()[-1] if out.strip() else ""
    msg = f"{which}: exit {code} in {time.time() - t0:.1f}s: {tail[:200]}"
    if code == -1 or not xml.exists():
        return None, msg
    import xml.etree.ElementTree as ET
    res: dict[str, str] = {}
    for tc in ET.parse(xml).getroot().iter("testcase"):
        tid = f"{tc.get('classname') or ''}::{tc.get('name')}"
        kinds = {c.tag for c in tc}
        res[tid] = ("failed" if "failure" in kinds else "error" if "error" in kinds
                    else "skipped" if "skipped" in kinds else "passed")
    return res, msg


def run_pytest(tree: Path, which: str, log: Path) -> tuple[bool, str]:
    """Strict: every test in the suite passes (used by case_checks `tests`)."""
    res, msg = pytest_results(tree, which, log)
    ok = res is not None and all(v in ("passed", "skipped") for v in res.values())
    return ok, msg


# ---------------------------------------------------------------- regression baseline (C16)

def per_bug_fixes(case: Case) -> dict[str, str]:
    """Per-bug fix patches (buggy -> fixed for one bug): private/<ID>/fixes/<bug-id>.patch,
    else reference.patch split by file using each bug's `files` (fails if bugs share a file)."""
    fixes_dir = case.private / "fixes"
    ids = [b["id"] for b in case.bugs]
    if fixes_dir.is_dir() and all((fixes_dir / f"{i}.patch").exists() for i in ids):
        return {i: (fixes_dir / f"{i}.patch").read_text() for i in ids}
    ref = case.reference_patch()
    if ref is None:
        raise GraderError(f"{case.id}: no fixes/ and no reference.patch")
    if len(ids) == 1:
        return {ids[0]: ref.read_text()}
    owners: dict[str, list[str]] = {}
    for b in case.bugs:
        for f in b.get("files") or []:
            owners.setdefault(f if f.startswith("src/") else "src/" + f, []).append(b["id"])
    out = {i: "" for i in ids}
    for path, text in split_patch(ref.read_text()):
        own = owners.get(path, [])
        if len(own) != 1:
            raise GraderError(f"{case.id}: can't attribute reference.patch hunks for {path} "
                              f"(owned by {own or 'no bug'}); add private/{case.id}/fixes/<bug-id>.patch")
        out[own[0]] += text
    return out


def proper_subsets(ids: list[str]) -> list[tuple[str, ...]]:
    """Every non-empty proper subset, smallest first (2 bugs: 2, 3 bugs: 6, 4 bugs: 14)."""
    from itertools import combinations
    return [c for k in range(1, len(ids)) for c in combinations(ids, k)]


BASELINE_VERSION = "c22-partial-variants"


def baseline_key(case: Case) -> str:
    return _tree_digest(case.dir / "src", case.dir / "tests", case.regression, case.dir / "sim",
                        case.dir / "scenarios", case.private / "scenarios", case.private / "fixes",
                        case.private / "reference.patch") + "-" + BASELINE_VERSION[:3]


def _variant_passing(case: Case, work: Path, name: str, patches: list[str]) -> tuple[dict, dict]:
    tree = work / f"baseline-{name}"
    if tree.exists():
        rmtree(tree)
    build_eval_tree(case, case.dir / "src", tree)
    for p in patches:
        apply_patch(tree, p)
    passing, not_passing = {}, {}
    for which in ("tests", "regression"):
        res, msg = pytest_results(tree, which, work / f"baseline-{name}-{which}.log")
        if res is None:
            raise GraderError(f"{case.id}: baseline {which} run on variant {name} broke: {msg}")
        passing[which] = {t for t, v in res.items() if v == "passed"}
        not_passing[which] = sorted(t for t, v in res.items() if v != "passed")
    rmtree(tree)
    return passing, not_passing


def compute_baseline(case: Case, work: Path) -> dict:
    """C16 + C22: a test gates only if it passes on the unfixed code AND on every
    partial-fix variant (each proper subset of the per-bug fixes), because a correct
    partial fix can unmask another bug."""
    out = {"case": case.id, "key": baseline_key(case), "version": BASELINE_VERSION}
    passing, not_passing = _variant_passing(case, work, "unfixed", [])
    for which in ("tests", "regression"):
        out[which + "_not_passing"] = not_passing[which]
    gate = {w: set(passing[w]) for w in passing}
    variants = {}
    ids = [b["id"] for b in case.bugs]
    if len(ids) > 1:
        try:
            fixes = per_bug_fixes(case)
        except GraderError as e:
            fixes = None
            out["variants_note"] = f"partial variants skipped: {e}"
        if fixes:
            for sub in proper_subsets(ids):
                name = "+".join(sub)
                pv, _ = _variant_passing(case, work, name, [fixes[i] for i in sub if fixes[i].strip()])
                lost = {w: sorted(gate[w] - pv[w]) for w in gate}
                variants[name] = {w: lost[w] for w in lost if lost[w]}
                for w in gate:
                    gate[w] &= pv[w]
    for which in ("tests", "regression"):
        out[which] = sorted(gate[which])
        out[which + "_unmasked"] = sorted(set(passing[which]) - gate[which])
    out["variants"] = variants
    return out


def load_baseline(case: Case, work: Path) -> tuple[dict, str]:
    """private/<ID>/regression-baseline.json if current, else grader/.cache, else computed
    now and cached (clarifications C16)."""
    key = baseline_key(case)
    for p, label in ((case.private / "regression-baseline.json", "private"),
                     (GRADER / ".cache" / case.id / f"regression-baseline-{key}.json", "cache")):
        if p.exists():
            b = load_json(p)
            if b.get("key") == key:
                return b, label
    b = compute_baseline(case, work)
    write_json(GRADER / ".cache" / case.id / f"regression-baseline-{key}.json", b)
    return b, "computed"


# ---------------------------------------------------------------- reference message counts (I3)

def _tree_digest(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in paths:
        if p is None:
            continue
        p = Path(p)
        if p.is_file():
            h.update(p.name.encode() + b"\0" + p.read_bytes())
        else:
            for k, v in sorted(tree_files(p).items()):
                h.update(k.encode() + b"\0" + hashlib.sha256(v).digest())
    return h.hexdigest()[:24]


def reference_counts(case: Case, tree: Path, bug: dict, seeds: list[int], work: Path,
                     workers: int) -> tuple[dict[int, int] | None, str]:
    """Reference message counts for I3: private/<ID>/refcounts.json if present,
    else computed by replaying the reference (src + reference.patch) and cached
    in grader/.cache/."""
    bid = bug["id"]
    rc_file = case.private / "refcounts.json"
    if rc_file.exists():
        rc = load_json(rc_file).get(bid, {})
        if all(str(s) in rc for s in seeds):
            return {s: int(rc[str(s)]) for s in seeds}, "refcounts.json"
    ref = case.reference_patch()
    if ref is None:
        return None, "no refcounts.json and no reference.patch: I3 not checked"
    scen = tree / "scenarios" / (case.scenario_name(bug) + ".yaml")
    key = _tree_digest(case.dir / "src", ref, scen, case.dir / "sim")
    cache = GRADER / ".cache" / case.id / f"refcounts-{bid}-{key}.json"
    have = load_json(cache) if cache.exists() else {}
    missing = [s for s in seeds if str(s) not in have]
    if missing:
        rtree = work / "reference-tree"
        if not rtree.exists():
            build_eval_tree(case, case.dir / "src", rtree)
            apply_patch(rtree, ref)
        def one(seed):
            out = work / "reference-runs" / bid / str(seed)
            res = simcli.replay(rtree, case.scenario_name(bug), seed, out)
            if not res.ok:
                raise GraderError(f"{case.id}: reference replay failed for {bid} seed {seed}: {res.error}")
            return seed, simcli.count_messages(out / "trace.jsonl")
        with ThreadPoolExecutor(max(1, workers)) as ex:
            for seed, n in ex.map(one, missing):
                have[str(seed)] = n
        write_json(cache, have)
    return {s: int(have[str(s)]) for s in seeds}, "reference replay (cached)"


# ---------------------------------------------------------------- scenarios

def merge_expect(tree: Path, scenario: str, expect: dict) -> None:
    """Fold the incident's per-bug `expect` block into the scenario file's own
    `expect` (incident keys win), so the runtime records it in the trace header
    and `sim check` evaluates it together with the codebase's EXPECT extras."""
    if not expect:
        return
    from common import yaml
    p = tree / "scenarios" / f"{scenario}.yaml"
    sc = load_yaml(p) or {}
    merged = dict(sc.get("expect") or {})
    merged.update(expect)
    sc["expect"] = merged
    p.write_text(yaml.safe_dump(sc, sort_keys=False))


def run_seed(tree: Path, scenario: str, seed: int, out: Path, expect: dict, ref_msgs: int | None,
             runs: int) -> dict:
    rec = {"seed": seed, "passed": False}
    traces = []
    for k in range(runs):
        o = out / f"r{k + 1}"
        res = simcli.replay(tree, scenario, seed, o)
        if not res.ok:
            rec["reason"] = f"replay {k + 1} failed: {res.error}"
            return rec
        traces.append((o / "trace.jsonl").read_bytes())
    if any(t != traces[0] for t in traces[1:]):
        rec["reason"] = "nondeterministic: traces differ between runs"
        return rec
    o = out / "r1"
    chk = simcli.check(tree, o, reference_messages=ref_msgs)
    if chk.error:
        rec["reason"] = f"invariant checker failed: {chk.error}"
        return rec
    violations = list(chk.violations)
    msgs = simcli.count_messages(o / "trace.jsonl")
    rec["messages"] = msgs
    if ref_msgs is not None:
        rec["reference_messages"] = ref_msgs
        if msgs > GOODPUT_FACTOR * ref_msgs and not any(str(v.get("invariant", "")).startswith("I3")
                                                         for v in violations):
            violations.append({"invariant": "I3", "t": None,
                               "detail": f"{msgs} messages > {GOODPUT_FACTOR}x reference {ref_msgs}"})
    # The scenario's expect block (with the incident's merged in, see merge_expect)
    # is evaluated by the checker; its violations carry invariant "expect".
    exp_fail = [v for v in violations if v.get("invariant") == "expect"]
    violations = [v for v in violations if v.get("invariant") != "expect"]
    if violations:
        rec["violations"] = violations[:5]
        rec["n_violations"] = len(violations)
    if exp_fail:
        rec["expect_failures"] = exp_fail[:5]
    if violations or exp_fail:
        first = violations[0] if violations else None
        rec["reason"] = (f"{first.get('invariant')}: {str(first.get('detail'))[:160]}" if first
                         else f"expect: {exp_fail[0]}")
        return rec
    rec["passed"] = True
    return rec


def evaluate(case: Case, src_dir: Path, work: Path, *, workers: int = 2, runs_per_seed: int = 2,
             skip_tests: bool = False, seeds_limit: int | None = None) -> dict:
    t0 = time.time()
    src_dir = Path(src_dir)
    patch_text, patch_files = diff_trees(case.dir / "src", src_dir)
    result: dict = {
        "patch_sha256": sha256_text(patch_text),
        "patch_files": patch_files,
    }
    tree = work / "tree"
    if tree.exists():
        rmtree(tree)
    result.update(build_eval_tree(case, src_dir, tree))
    notes: list[str] = []

    if skip_tests:
        result["tests_ok"] = result["regression_ok"] = None
    else:
        # C16: the gate is relative to the unfixed code. Only tests that pass on the
        # unfixed case code can zero the case; tests the bugs break are reported only.
        base, how = load_baseline(case, work)
        notes.append(f"regression baseline: {how}")
        result["baseline_regressions"] = []
        for which, key in (("tests", "tests_ok"), ("regression", "regression_ok")):
            res, msg = pytest_results(tree, which, work / f"{which}.log")
            notes.append(msg)
            res = res or {}
            broke = [t for t in base.get(which, []) if res.get(t) != "passed"]
            result[key] = not broke
            result["baseline_regressions"] += [f"{which}: {t}" for t in broke]
            result[f"{which}_failed"] = sorted(t for t, v in res.items() if v in ("failed", "error"))
            result[f"{which}_passed"] = sum(v == "passed" for v in res.values())
        if case.regression is None:
            notes.append("no regression/ directory found for this case")

    bugs_out = []
    raw_fixed: dict[str, bool] = {}
    for bug in case.bugs:
        bid = bug["id"]
        seeds = case.bug_seeds(bid)
        if seeds_limit:
            seeds = seeds[:seeds_limit]
        scenario = case.scenario_name(bug)
        if not (tree / "scenarios" / f"{scenario}.yaml").exists():
            raise GraderError(f"{case.id}: scenario {scenario}.yaml for {bid} not found in private/ or case")
        refs, how = reference_counts(case, tree, bug, seeds, work, workers)
        notes.append(f"{bid} I3 reference: {how}")
        expect = dict(bug.get("expect") or {})
        merge_expect(tree, scenario, expect)

        def job(seed, _bid=bid, _scen=scenario, _exp=expect, _refs=refs):
            return run_seed(tree, _scen, seed, work / "runs" / _bid / str(seed), _exp,
                            _refs.get(seed) if _refs else None, runs_per_seed)
        with ThreadPoolExecutor(max(1, workers)) as ex:
            recs = list(ex.map(job, seeds))
        passed = sum(r["passed"] for r in recs)
        raw_fixed[bid] = passed == len(seeds)
        bugs_out.append({"id": bid, "fixed": False, "seeds_passed": passed, "seeds_total": len(seeds),
                         "scenario_passed": raw_fixed[bid],
                         "gates": list(bug.get("gates") or []),
                         "failures": [{k: v for k, v in r.items() if k != "passed"}
                                      for r in recs if not r["passed"]][:5]})

    tests_pass = skip_tests or (result["tests_ok"] and result["regression_ok"])
    fixed: dict[str, bool] = {}

    def is_fixed(bid, stack=()):
        if bid in fixed:
            return fixed[bid]
        if bid in stack:
            raise GraderError(f"{case.id}: gate cycle through {bid}")
        b = next(x for x in bugs_out if x["id"] == bid)
        ok = raw_fixed[bid] and all(is_fixed(g, stack + (bid,)) for g in b["gates"])
        fixed[bid] = ok
        return ok

    for b in bugs_out:
        b["fixed"] = bool(tests_pass and is_fixed(b["id"]))
        if not b["gates"]:
            b.pop("gates")
    result["bugs"] = bugs_out
    result["bugs_fixed"] = sum(b["fixed"] for b in bugs_out)
    result["notes"] = notes
    result["eval_s"] = round(time.time() - t0, 2)
    (work / "patch.diff").write_text(patch_text)
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--src", required=True, help="candidate src/ directory")
    ap.add_argument("--out", required=True, help="JSON result path")
    ap.add_argument("--private-dir")
    ap.add_argument("--codebases-root")
    ap.add_argument("--work", help="scratch dir (kept); default: temp, removed")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--runs-per-seed", type=int, default=2)
    ap.add_argument("--seeds-limit", type=int)
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args(argv)
    work = Path(a.work) if a.work else Path(tempfile.mkdtemp(prefix="c3eval-"))
    try:
        case = Case(Path(a.case_dir), a.private_dir, a.codebases_root)
        res = evaluate(case, Path(a.src), work, workers=a.workers, runs_per_seed=a.runs_per_seed,
                       skip_tests=a.skip_tests, seeds_limit=a.seeds_limit)
        res["eval_error"] = None
        write_json(Path(a.out), res)
        return 0
    except GraderError as e:
        write_json(Path(a.out), {"eval_error": str(e)})
        print(f"evaluate: {e}", file=sys.stderr)
        return 2
    finally:
        if not a.work:
            rmtree(work)


if __name__ == "__main__":
    sys.exit(main())
