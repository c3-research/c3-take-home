#!/usr/bin/env python3
"""Per-case automated checks T7-T12.

    python grader/case_checks.py <check> (--all | ID [ID ...]) [--workers 4]

checks:
  floor        T7  unfixed code: at most 20% of the case's bugs pass their scenarios
  ceiling      T8  reference: every bug fixed and tests pass, in each of 3 runs
  attribution  T9  reference minus one bug's fix fails exactly that bug plus the bugs it gates
  tests        T10 public tests pass on unfixed and reference code; regression passes on reference
  signature    T11 signature.json holds on at least one public-seed replay of public.yaml
  corpus-fact  T12 each corpus fact is in the corpus only, and its pointer token is in logs/
  all          every check above
  baseline     compute and cache the C16/C22 regression baseline (tests passing on the unfixed
               code and on every partial-fix variant)
  partial      C22: every single-fix and pair variant earns exactly the credit of the bugs it fixes

--all covers every private/<ID>/ with an incident.yaml (fails if there are none).
Exit 0 only if every selected case passes. Details are written to
grader/.cache/checks/<check>.json (and to $C3_EVIDENCE_DIR/case-checks-<check>.json when set).

Per-bug fix patches for attribution: private/<ID>/fixes/<bug-id>.patch if present,
otherwise reference.patch is split by file using each bug's `files` list (which only
works when bugs don't share a file; shared-file cases need fixes/<bug-id>.patch).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (GRADER, ROOT, GraderError, apply_patch, copy_tree, load_yaml, rmtree,  # noqa: E402
                    split_patch, tree_files, write_json)
import simcli  # noqa: E402
from evaluate import Case, build_eval_tree, evaluate, per_bug_fixes, proper_subsets, run_pytest  # noqa: E402
from signature import load_signature, match  # noqa: E402

THRESH = {"floor_max_fraction_bugs_passing": 0.20, "reference_runs": 3}


def load_thresholds():
    p = ROOT / "contracts" / "thresholds.yaml"
    if p.exists():
        try:
            t = load_yaml(p).get("cases", {})
            THRESH.update({k: t[k] for k in THRESH if k in t})
        except Exception:
            pass


# ---------------------------------------------------------------- helpers

class Ctx:
    def __init__(self, a):
        self.a = a
        self.private_root = Path(a.private_root) if a.private_root else ROOT / "private"
        self.cases_root = Path(a.cases_root) if a.cases_root else ROOT / "cases"
        self.codebases_root = Path(a.codebases_root) if a.codebases_root else None
        self.corpus_root = Path(a.corpus_root) if a.corpus_root else ROOT / "corpus"
        self.workers = a.workers

    def case(self, cid: str) -> Case:
        for split in ("practice", "hidden"):
            d = self.cases_root / split / cid
            if (d / "case.json").exists():
                return Case(d, self.private_root / cid, self.codebases_root)
        if (self.cases_root / cid / "case.json").exists():
            return Case(self.cases_root / cid, self.private_root / cid, self.codebases_root)
        raise GraderError(f"{cid}: public case not built (run grader/make_case.py {cid})")


def src_variant(case: Case, work: Path, name: str, patches: list[str | Path]) -> Path:
    """case src/ with the given patches applied; returns the src dir."""
    root = work / "variants" / name
    if root.exists():
        rmtree(root)
    copy_tree(case.dir / "src", root / "src")
    for p in patches:
        apply_patch(root, p)
    return root / "src"


def gated_by(case: Case, bid: str) -> set[str]:
    """Bugs that transitively need `bid` fixed first."""
    out, frontier = set(), {bid}
    while frontier:
        nxt = {b["id"] for b in case.bugs if set(b.get("gates") or []) & frontier} - out
        out |= nxt
        frontier = nxt
    return out


def summarize_eval(res: dict) -> dict:
    return {"tests_ok": res.get("tests_ok"), "regression_ok": res.get("regression_ok"),
            "bugs": [{k: b.get(k) for k in ("id", "fixed", "scenario_passed", "seeds_passed", "seeds_total")}
                     | ({"first_failure": b["failures"][0]} if b.get("failures") else {})
                     for b in res["bugs"]]}


# ---------------------------------------------------------------- checks

def check_floor(ctx, case, work):
    res = evaluate(case, case.dir / "src", work, workers=ctx.workers, skip_tests=True)
    frac = res["bugs_fixed"] / len(case.bugs)
    ok = frac <= THRESH["floor_max_fraction_bugs_passing"] + 1e-9
    return ok, f"unfixed code passes {res['bugs_fixed']}/{len(case.bugs)} bugs ({frac:.0%})", summarize_eval(res)


def check_ceiling(ctx, case, work):
    ref = case.reference_patch()
    if ref is None:
        return False, "no reference.patch", {}
    src = src_variant(case, work, "reference", [ref])
    runs, ok, sigs = [], True, set()
    for k in range(int(THRESH["reference_runs"])):
        res = evaluate(case, src, work / f"run{k + 1}", workers=ctx.workers)
        # strict for the reference: every public and regression test passes
        good = bool(res["tests_ok"] and res["regression_ok"] and not res.get("tests_failed")
                    and not res.get("regression_failed") and res["bugs_fixed"] == len(case.bugs))
        ok &= good
        sigs.add(json.dumps([(b["id"], b["seeds_passed"]) for b in res["bugs"]]))
        runs.append(summarize_eval(res))
    if len(sigs) > 1:
        ok = False
    fixed = [sum(b["fixed"] for b in r["bugs"]) for r in runs]
    return ok, f"reference fixes {fixed} of {len(case.bugs)} bugs over {len(runs)} runs" + \
        ("" if len(sigs) == 1 else " (runs disagree)"), {"runs": runs}


def check_attribution(ctx, case, work):
    fixes = per_bug_fixes(case)
    ids = [b["id"] for b in case.bugs]
    details, ok, lines = {}, True, []
    for bid in ids:
        others = [fixes[i] for i in ids if i != bid and fixes[i].strip()]
        src = src_variant(case, work, f"minus-{bid}", others)
        res = evaluate(case, src, work / f"minus-{bid}", workers=ctx.workers, skip_tests=True)
        failed = {b["id"] for b in res["bugs"] if not b["fixed"]}
        want = {bid} | gated_by(case, bid)
        good = failed == want
        ok &= good
        lines.append(f"-{bid}: fails {sorted(failed)}" + ("" if good else f" (expected {sorted(want)})"))
        details[bid] = summarize_eval(res) | {"expected_fail": sorted(want)}
    return ok, "; ".join(lines), details


def check_tests(ctx, case, work):
    ref = case.reference_patch()
    out, ok = {}, True
    t_unfixed = work / "unfixed"
    build_eval_tree(case, case.dir / "src", t_unfixed)
    good, msg = run_pytest(t_unfixed, "tests", work / "unfixed-tests.log")
    out["unfixed_tests"] = msg
    ok &= good
    if ref is None:
        return False, "no reference.patch", out
    t_ref = work / "reference"
    build_eval_tree(case, src_variant(case, work, "reference", [ref]), t_ref)
    good_t, msg_t = run_pytest(t_ref, "tests", work / "ref-tests.log")
    if case.regression is None:
        good_r, msg_r = False, "no regression/ directory"
    else:
        good_r, msg_r = run_pytest(t_ref, "regression", work / "ref-regression.log")
    out.update(reference_tests=msg_t, reference_regression=msg_r)
    ok &= good_t and good_r
    return ok, " | ".join(out.values()), out


def check_signature(ctx, case, work):
    sp = case.private / "signature.json"
    if not sp.exists():
        return False, "no signature.json", {}
    sig = load_signature(sp)
    tree = work / "public"
    copy_tree(case.dir, tree)
    hits, reports = [], {}
    for s in [int(x) for x in case.meta.get("public_seeds", [])]:
        out = work / "replays" / str(s)
        r = simcli.replay(tree, "public", s, out)
        if not r.ok:
            reports[str(s)] = [f"replay failed: {r.error}"]
            continue
        good, rep = match(sig, simcli.load_trace(out / "trace.jsonl"))
        reports[str(s)] = rep
        if good:
            hits.append(s)
    ok = bool(hits)
    return ok, f"signature holds on public seeds {hits}" if ok else "signature holds on no public seed", \
        {"seeds": reports, "matching_seeds": hits}


# Fact tokens that must not leak into public files: key=value settings and
# numbers with units (250ms, 0.4s, 200ppm). Plain identifiers are expected in code.
_DISTINCT = re.compile(r"[A-Za-z_][\w.\-]*=[\w.\-]+|\b\d+(?:\.\d+)?(?:ms|s|us|ppm|%|x)\b")


def _front_matter_target(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    for line in text.split("\n")[1:40]:
        if line.strip() == "---":
            break
        if line.startswith("target:"):
            return line.split(":", 1)[1].split("#")[0].strip()
    return None


def _resolve_location(ctx, case, location: str) -> tuple[Path | None, str | None, str]:
    """Find the fact's document. Built corpus first build/grading,
    build/practice), then the source layer, then incident drafts whose front matter
    targets the same file (not yet merged)."""
    path, _, anchor = location.partition("#")
    rel = path[len("corpus/"):] if path.startswith("corpus/") else path
    practice = case.codebase in ("A", "B")
    variants = ["practice", "grading"] if practice else ["grading"]
    for base, label in [(ctx.corpus_root / "build" / v, f"build/{v}") for v in variants] + \
            [(ctx.corpus_root / "build", "build"), (ctx.corpus_root, "source")]:
        p = base / rel
        if p.is_file():
            return p, anchor or None, label
    drafts = ctx.corpus_root / "incidents" / case.id
    if drafts.is_dir():
        for p in sorted(drafts.rglob("*.md")):
            if _front_matter_target(p.read_text(errors="replace")) == rel:
                return p, anchor or None, "incident draft (not yet merged)"
    return None, anchor or None, "missing"


def _slug(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.strip().lower())
    return re.sub(r"[\s]+", "-", s)


def _has_anchor(text: str, anchor: str) -> bool:
    anchor = anchor.lower()
    for line in text.splitlines():
        if line.startswith("#") and _slug(line.lstrip("#")) == anchor:
            return True
        if f'id="{anchor}"' in line.lower() or f"{{#{anchor}}}" in line.lower():
            return True
    return False


def check_corpus_fact(ctx, case, work):
    lines, ok, details = [], True, {}
    public = tree_files(case.dir)
    logs_text = "\n".join(v.decode(errors="replace") for k, v in public.items() if k.startswith("logs/"))
    non_log = {k: v.decode(errors="replace") for k, v in public.items() if not k.startswith("logs/")}
    for bug in case.bugs:
        cf = bug.get("corpus_fact")
        bid = bug["id"]
        if not cf:
            lines.append(f"{bid}: no corpus_fact")
            details[bid] = {"ok": None}
            continue
        n_facts = details.setdefault("_n_facts", 0) + 1
        details["_n_facts"] = n_facts
        problems = []
        loc, anchor, where = _resolve_location(ctx, case, cf.get("location", ""))
        text = loc.read_text(errors="replace") if loc else ""
        if loc is None:
            problems.append(f"location {cf.get('location')} not found in corpus")
        elif anchor and not _has_anchor(text, anchor):
            problems.append(f"anchor #{anchor} not found in {loc}")
        ptr = cf.get("pointer_in_logs")
        if not ptr:
            problems.append("no pointer_in_logs")
        else:
            if ptr not in logs_text:
                problems.append(f"pointer {ptr!r} not in logs/")
            if loc is not None and ptr not in text:
                problems.append(f"pointer {ptr!r} not in the corpus document {loc.name}")
        stmt = " ".join(str(cf.get("statement", "")).split())
        if stmt:
            low = stmt.lower()
            for k, v in non_log.items():
                if low in " ".join(v.split()).lower():
                    problems.append(f"statement appears verbatim in public {k}")
            for tok in sorted(set(_DISTINCT.findall(stmt))):
                if tok == ptr or len(tok) < 4:
                    continue
                leaks = [k for k, v in non_log.items() if tok in v and not k.startswith(("sim/", "tests/"))]
                if leaks:
                    problems.append(f"fact token {tok!r} appears in public {leaks[:3]}")
        good = not problems
        ok &= good
        details[bid] = {"ok": good, "location": str(loc) if loc else None, "where": where,
                        "problems": problems}
        lines.append(f"{bid}: {'ok' if good else '; '.join(problems)} [{where}]")
    if not details.get("_n_facts"):
        ok = False
        lines.append("case has no corpus_fact (case-layout: every case has at least one)")
    return ok, " | ".join(lines), details


def check_baseline(ctx, case, work):
    """Not a T-check: compute (or load) the C16/C22 regression baseline and cache it."""
    from evaluate import load_baseline
    b, how = load_baseline(case, work)
    unm = len(b.get("tests_unmasked", [])) + len(b.get("regression_unmasked", []))
    msg = (f"[{how}] gating: {len(b['tests'])} tests, {len(b['regression'])} regression; "
           f"fail on unfixed: {len(b['tests_not_passing'])}+{len(b['regression_not_passing'])}; "
           f"dropped by partial variants: {unm}")
    if b.get("variants"):
        msg += " (" + "; ".join(f"{k}: {sum(len(x) for x in v.values())}" for k, v in b["variants"].items() if v) + ")"
    if b.get("variants_note"):
        msg += f" NOTE {b['variants_note']}"
    return True, msg, {k: b.get(k) for k in ("tests_unmasked", "regression_unmasked", "variants",
                                              "tests_not_passing", "regression_not_passing")}


def expected_credit(case: Case, subset) -> set[str]:
    """Bugs a variant fixing `subset` should be credited with: in the subset and every
    gate (transitively) also fixed."""
    by = {b["id"]: b for b in case.bugs}
    done: dict[str, bool] = {}

    def ok(bid):
        if bid not in done:
            done[bid] = bid in subset and all(ok(g) for g in (by[bid].get("gates") or []))
        return done[bid]
    return {i for i in by if ok(i)}


def check_partial(ctx, case, work):
    """C22: each single-fix and pair variant earns exactly the credit of the bugs it fixes."""
    ids = [b["id"] for b in case.bugs]
    if len(ids) < 2:
        return True, "single-bug case: nothing to check", {}
    fixes = per_bug_fixes(case)
    subs = [s for s in proper_subsets(ids) if len(s) <= 2]
    ok, lines, det = True, [], {}
    for sub in subs:
        name = "+".join(sub)
        src = src_variant(case, work, name, [fixes[i] for i in sub if fixes[i].strip()])
        res = evaluate(case, src, work / f"eval-{name}", workers=ctx.workers)
        got = {b["id"] for b in res["bugs"] if b["fixed"]}
        want = expected_credit(case, set(sub))
        good = got == want
        ok &= good
        lines.append(f"{name}: {sorted(got)}" + ("" if good else
                     f" (expected {sorted(want)}; baseline_regressions={res.get('baseline_regressions')})"))
        det[name] = summarize_eval(res) | {"expected": sorted(want),
                                             "baseline_regressions": res.get("baseline_regressions")}
    return ok, "; ".join(lines), det


CHECKS = {"floor": check_floor, "ceiling": check_ceiling, "attribution": check_attribution,
          "tests": check_tests, "signature": check_signature, "corpus-fact": check_corpus_fact}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("check", choices=list(CHECKS) + ["all", "baseline", "partial"])
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--private-root")
    ap.add_argument("--cases-root")
    ap.add_argument("--codebases-root")
    ap.add_argument("--corpus-root")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args(argv)
    load_thresholds()
    ctx = Ctx(a)
    if a.all:
        ids = sorted(p.name for p in ctx.private_root.iterdir()
                     if (p / "incident.yaml").exists()) if ctx.private_root.is_dir() else []
        if not ids:
            print(f"no cases under {ctx.private_root} (nothing to check)")
            return 1
    else:
        ids = a.ids
    if not ids:
        ap.error("give case IDs or --all")
    checks = list(CHECKS) if a.check == "all" else [a.check]
    CHECKS_ALL = dict(CHECKS, baseline=check_baseline, partial=check_partial)
    all_ok = True
    for chk in checks:
        report = {"check": chk, "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "cases": {}}
        for cid in ids:
            work = Path(tempfile.mkdtemp(prefix=f"c3b2-check-{chk}-{cid}-", dir=os.environ.get("C3_CHECK_TMP")))
            t0 = time.time()
            try:
                case = ctx.case(cid)
                ok, msg, det = CHECKS_ALL[chk](ctx, case, work)
            except GraderError as e:
                ok, msg, det = False, f"error: {e}", {}
            except Exception as e:  # keep going over the other cases
                import traceback
                ok, msg, det = False, f"crash: {e!r}", {"traceback": traceback.format_exc()}
            finally:
                if not a.keep:
                    rmtree(work)
            all_ok &= ok
            report["cases"][cid] = {"passed": ok, "summary": msg, "seconds": round(time.time() - t0, 1),
                                    "details": det}
            print(f"{chk:12} {cid:4} {'PASS' if ok else 'FAIL'}  {msg}", flush=True)
        report["passed"] = all(c["passed"] for c in report["cases"].values())
        write_json(GRADER / ".cache" / "checks" / f"{chk}.json", report)
        if os.environ.get("C3_EVIDENCE_DIR"):
            write_json(Path(os.environ["C3_EVIDENCE_DIR"]) / f"case-checks-{chk}.json", report)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
