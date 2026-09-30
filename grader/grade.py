#!/usr/bin/env python3
"""C3 assessment grader.

    python grader/grade.py --fixer <image-or-fixer-dir> --cases <dir-or-ids> \\
        [--mode docker|process] [--concurrency 6] [--runs 1] --out <results-dir>

For each case and run: copy the public case to a fresh workspace, mint a proxy
token with the case's budget_usd, run the fixer until it exits or its deadline
(budget_s) passes (SIGTERM, then SIGKILL 5 s later), keep only src/, and score it
in a separate process (grader/evaluate.py) against the private case. Writes
<out>/<case>/result.json (with --runs N > 1: <out>/<case>/run-<n>/result.json,
and <out>/summary.json.

--cases accepts a case directory, a directory of cases (cases/practice), or
case IDs (A1,B2 or "A1 B2"), looked up under cases/practice and cases/hidden.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (GRADER, PRACTICE_CODEBASES, ROOT, GraderError, copy_tree, find_case_dirs,  # noqa: E402
                    find_private_dir, load_json, rmtree, write_json)
import fixer_runner as fr  # noqa: E402
from proxy_client import ProxyInfraError, make_proxy  # noqa: E402

GLOBAL_MAX_CONCURRENCY = 6
_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def default_corpus(codebase: str | None) -> Path:
    """practice cases see corpus/build/practice, hidden cases
    corpus/build/grading."""
    variant = "practice" if codebase in PRACTICE_CODEBASES else "grading"
    for p in (ROOT / "corpus" / "build" / variant, ROOT / "corpus" / "build", ROOT / "corpus"):
        if p.is_dir():
            return p.resolve()
    return Path(tempfile.mkdtemp(prefix="c3-empty-corpus-"))


def grade_one(a, spec: fr.FixerSpec, proxy, case_dir: Path, run: int, out_dir: Path) -> dict:
    meta = load_json(case_dir / "case.json")
    cid = meta["id"]
    budget_s = float(a.budget_s if a.budget_s is not None else meta.get("budget_s", 900))
    budget_usd = float(meta.get("budget_usd", 3.0))
    fixer_id = a.fixer_id or spec.id
    corpus = a.corpus or default_corpus(meta.get("codebase"))
    result = {"case": cid, "fixer": fixer_id, "run": run, "mode": a.mode_used,
              "fixer_exit": None, "timed_out": False, "wall_s": 0.0,
              "spend_usd": 0.0, "cached_input_tokens": 0, "input_tokens": 0, "output_tokens": 0,
              "patch_sha256": None, "patch_files": [],
              "tests_ok": False, "regression_ok": False,
              "bugs": [], "bugs_fixed": 0, "infra_error": None,
              "baseline_regressions": [], "tests_failed": [], "regression_failed": []}
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f"c3grade-{cid}-r{run}-", dir=a.tmp))
    tok = None
    try:
        private = find_private_dir(cid, a.private_root, case_dir)
        ws = scratch / "workspace"
        copy_tree(case_dir, ws)
        os.chmod(ws, 0o777 if spec.kind == "image" else 0o755)
        if spec.kind == "image":
            for p in ws.rglob("*"):
                os.chmod(p, 0o777 if p.is_dir() else 0o666)
        # 1. proxy token
        try:
            proxy.health()
            tok = proxy.mint(cid, fixer_id, a.arm, budget_usd)
        except ProxyInfraError as e:
            result["infra_error"] = f"proxy: {e}"
            return result
        # 2. fixer
        penv = proxy.fixer_env(tok, docker=(spec.kind == "image"))
        log(f"{cid} run {run}: fixer {fixer_id} started (budget {budget_s:.0f}s, ${budget_usd:.2f})")
        try:
            if spec.kind == "image":
                oc = fr.run_docker(spec, workspace=ws, corpus=corpus, budget_s=budget_s,
                                   budget_usd=budget_usd, proxy_env=penv, scratch=scratch,
                                   log_path=out_dir / "fixer.log", grace=a.kill_grace)
            else:
                oc = fr.run_process(spec, workspace=ws, corpus=corpus, budget_s=budget_s,
                                    budget_usd=budget_usd, proxy_env=penv, scratch=scratch,
                                    log_path=out_dir / "fixer.log", sandbox=a.sandbox,
                                    grace=a.kill_grace)
        except fr.FixerInfraError as e:
            result["infra_error"] = f"sandbox: {e}"
            return result
        result.update(fixer_exit=oc.exit_code, timed_out=oc.timed_out, wall_s=oc.wall_s)
        if oc.killed:
            result["killed"] = True
        # proxy still up and upstream healthy?
        try:
            proxy.health()
        except ProxyInfraError as e:
            result["infra_error"] = f"proxy down after run: {e}"
        up = proxy.upstream_failure(tok)
        if up and not result["infra_error"]:
            result["infra_error"] = f"openrouter: {up}"
        u = proxy.usage(tok)
        result.update(spend_usd=round(u.spend_usd, 6), cached_input_tokens=u.cached_input_tokens,
                      input_tokens=u.input_tokens, output_tokens=u.output_tokens)
        proxy.close(tok)
        tok = None
        log(f"{cid} run {run}: fixer exit {oc.exit_code}{' (timed out)' if oc.timed_out else ''} "
            f"after {oc.wall_s:.1f}s; grading")
        # 3. score src/ only, in a separate process
        src = ws / "src"
        if not src.is_dir():
            src.mkdir()
        eval_json = out_dir / "eval.json"
        argv = [sys.executable, str(GRADER / "evaluate.py"), "--case-dir", str(case_dir),
                "--src", str(src), "--out", str(eval_json), "--private-dir", str(private),
                "--workers", str(a.eval_workers)]
        if a.codebases_root:
            argv += ["--codebases-root", str(a.codebases_root)]
        if a.keep:
            argv += ["--work", str(out_dir / "eval-work")]
        r = subprocess.run(argv, capture_output=True, text=True,
                           env={k: v for k, v in os.environ.items() if not k.startswith(("C3_PROXY", "OPENROUTER", "ANTHROPIC", "OPENAI"))})
        ev = load_json(eval_json) if eval_json.exists() else {"eval_error": f"evaluate.py exit {r.returncode}: {r.stderr[-500:]}"}
        if ev.get("eval_error"):
            result["infra_error"] = result["infra_error"] or f"grader: {ev['eval_error']}"
            return result
        result.update(patch_sha256=ev["patch_sha256"], patch_files=ev["patch_files"],
                      tests_ok=ev["tests_ok"], regression_ok=ev["regression_ok"],
                      bugs=[{k: b[k] for k in ("id", "fixed", "seeds_passed", "seeds_total")}
                            for b in ev["bugs"]],
                      bugs_fixed=ev["bugs_fixed"],
                      # C16: every test result is reported; only baseline_regressions gate.
                      baseline_regressions=ev.get("baseline_regressions", []),
                      tests_failed=ev.get("tests_failed", []),
                      regression_failed=ev.get("regression_failed", []))
        if (out_dir / "eval-work" / "patch.diff").exists():
            (out_dir / "patch.diff").write_bytes((out_dir / "eval-work" / "patch.diff").read_bytes())
        else:
            from common import diff_trees
            (out_dir / "patch.diff").write_text(diff_trees(case_dir / "src", src)[0])
        return result
    except GraderError as e:
        result["infra_error"] = f"grader: {e}"
        return result
    except Exception as e:  # never lose a case silently
        result["infra_error"] = f"grader crash: {e!r}"
        (out_dir / "grader-traceback.txt").write_text(traceback.format_exc())
        return result
    finally:
        if tok is not None:
            try:
                proxy.close(tok)
            except Exception:
                pass
        if not a.keep:
            rmtree(scratch)
        write_json(out_dir / "result.json", result)
        status = "INFRA ERROR: " + result["infra_error"] if result["infra_error"] else \
            f"{result['bugs_fixed']}/{len(result['bugs'])} bugs fixed, tests_ok={result['tests_ok']} " \
            f"regression_ok={result['regression_ok']}"
        log(f"{cid} run {run}: {status}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixer", required=True, help="fixer directory (process mode) or docker image")
    ap.add_argument("--cases", required=True, nargs="+")
    ap.add_argument("--mode", choices=["docker", "process", "local"],
                    help="docker: image; process: fixer dir inside sandbox/run.sh (bwrap); "
                         "local: fixer dir's run.sh as a plain subprocess, no sandbox (practice only, C15)")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fixer-id", help="label for results and the proxy ledger (default: dir or image name)")
    ap.add_argument("--arm", default="base", help="ledger label, e.g. base, no-corpus, serial")
    ap.add_argument("--proxy", choices=["auto", "http", "stub"],
                    help="model proxy: http = real proxy, a dead proxy is an infra_error (use for real grading); "
                         "auto (default, or $C3_PROXY) = http if it answers at start, else stub; stub = no proxy")
    ap.add_argument("--sandbox", choices=["auto", "on", "off"], default="auto",
                    help="process mode: wrap the fixer in sandbox/run.sh (auto = when it exists)")
    ap.add_argument("--corpus", type=Path, help="corpus dir mounted at /corpus (default corpus/build/practice or /grading)")
    ap.add_argument("--private-root", type=Path, help="dir holding <ID>/incident.yaml etc. (default private/)")
    ap.add_argument("--codebases-root", type=Path)
    ap.add_argument("--budget-s", type=float, help="override case budget_s (plumbing tests only)")
    ap.add_argument("--kill-grace", type=float, default=5.0)
    ap.add_argument("--eval-workers", type=int, default=2)
    ap.add_argument("--tmp", help="parent dir for scratch workspaces")
    ap.add_argument("--keep", action="store_true", help="keep workspaces and eval work dirs")
    a = ap.parse_args(argv)

    try:
        cases = [d for spec in a.cases for d in find_case_dirs(spec)]
    except GraderError as e:
        print(f"grade: {e}", file=sys.stderr)
        return 2
    mode = a.mode or ("process" if Path(a.fixer).is_dir() else "docker")
    out = Path(a.out).resolve()
    try:
        spec = fr.resolve_fixer(a.fixer, "process" if mode == "local" else mode)
        if mode == "local":
            a.sandbox = "off"
            print("grade: local mode: the fixer runs WITHOUT a sandbox (practice only; real grading "
                  "uses process or docker mode)", file=sys.stderr)
        elif mode == "process" and spec.kind == "dir" and a.sandbox == "auto" and fr.sandbox_script() is None:
            print("grade: WARNING: sandbox/run.sh not found; running the fixer without a sandbox", file=sys.stderr)
        if spec.kind == "image":
            if mode != "docker":
                raise GraderError("an image fixer needs --mode docker")
            fr.docker_image_check(spec.ref)
    except fr.PlatformError as e:
        # a candidate error, recorded per case, and a non-zero exit.
        for c in cases:
            meta = load_json(c / "case.json")
            try:
                from evaluate import Case
                bugs = [{"id": b["id"], "fixed": False, "seeds_passed": 0,
                         "seeds_total": len(Case(c, find_private_dir(meta["id"], a.private_root, c)).bug_seeds(b["id"]))}
                        for b in Case(c, find_private_dir(meta["id"], a.private_root, c)).bugs]
            except Exception:
                bugs = []
            res = {"case": meta["id"], "fixer": a.fixer_id or a.fixer, "run": 1, "fixer_exit": None,
                   "timed_out": False, "wall_s": 0.0, "spend_usd": 0.0, "cached_input_tokens": 0,
                   "input_tokens": 0, "output_tokens": 0, "patch_sha256": None, "patch_files": [],
                   "tests_ok": False, "regression_ok": False, "bugs": bugs, "bugs_fixed": 0,
                   "infra_error": None, "error": f"unsupported_platform: {e.arch}"}
            write_json(out / meta["id"] / "result.json", res)
        print(f"grade: unsupported_platform: {e.arch}: {e}", file=sys.stderr)
        return 1
    except GraderError as e:
        print(f"grade: {e}", file=sys.stderr)
        return 2
    a.corpus = a.corpus.resolve() if a.corpus else None
    a.mode_used = mode
    conc = max(1, min(a.concurrency, GLOBAL_MAX_CONCURRENCY))
    proxy = make_proxy(a.proxy)
    out.mkdir(parents=True, exist_ok=True)
    log(f"grading {len(cases)} case(s) x {a.runs} run(s) with {spec.id} ({mode}, proxy={proxy.name}, "
        f"concurrency={conc})")

    jobs = [(c, r) for r in range(1, a.runs + 1) for c in cases]

    def job(c_r):
        c, r = c_r
        cid = load_json(c / "case.json")["id"]
        d = out / cid if a.runs == 1 else out / cid / f"run-{r}"
        return grade_one(a, spec, proxy, c, r, d)

    t0 = time.time()
    with ThreadPoolExecutor(conc) as ex:
        results = list(ex.map(job, jobs))
    scored = [r for r in results if not r["infra_error"]]
    summary = {
        "fixer": a.fixer_id or spec.id, "mode": mode, "arm": a.arm, "runs": a.runs,
        "cases": len(cases), "bugs_fixed": sum(r["bugs_fixed"] for r in scored),
        "bugs_total": sum(len(r["bugs"]) for r in scored),
        "wall_s": round(sum(r["wall_s"] for r in scored), 1),
        "spend_usd": round(sum(r["spend_usd"] for r in results), 4),
        "infra_errors": [{"case": r["case"], "run": r["run"], "error": r["infra_error"]}
                         for r in results if r["infra_error"]],
        "grading_wall_s": round(time.time() - t0, 1),
        "results": [{k: r[k] for k in ("case", "run", "bugs_fixed", "wall_s", "spend_usd", "fixer_exit",
                                       "timed_out", "tests_ok", "regression_ok", "infra_error")}
                    for r in results],
    }
    write_json(out / "summary.json", summary)
    print()
    print(f"{'case':6} {'run':>3} {'fixed':>7} {'tests':>5} {'regr':>5} {'exit':>5} {'wall_s':>8} {'usd':>7}")
    for r in results:
        if r["infra_error"]:
            print(f"{r['case']:6} {r['run']:>3}  INFRA ERROR (not scored): {r['infra_error'][:100]}")
            continue
        print(f"{r['case']:6} {r['run']:>3} {r['bugs_fixed']:>3}/{len(r['bugs']):<3} {str(r['tests_ok'])[0]:>5} "
              f"{str(r['regression_ok'])[0]:>5} {str(r['fixer_exit']):>5} {r['wall_s']:>8.1f} {r['spend_usd']:>7.2f}"
              + ("  timed out" if r["timed_out"] else ""))
    print(f"\nscore: {summary['bugs_fixed']}/{summary['bugs_total']} bugs fixed, fixer time {summary['wall_s']:.0f}s, "
          f"spend ${summary['spend_usd']:.2f}; results in {out}")
    return 3 if summary["infra_errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
