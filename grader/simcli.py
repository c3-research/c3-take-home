"""Adapter over the case's `python -m sim` CLI (runtime.md section 9).

All calls run in fresh subprocesses with a scrubbed environment. The
invariant checker runs without src/ on sys.path: it reads only the trace.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from common import grader_python

REPLAY_TIMEOUT_S = float(os.environ.get("C3_REPLAY_TIMEOUT_S", 120))
CHECK_TIMEOUT_S = float(os.environ.get("C3_CHECK_TIMEOUT_S", 120))


def clean_env(tree: Path, *, with_src: bool) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HOME": str(tree),
    }
    parts = [str(tree)]
    if with_src:   # pytest: the service package and c3sim (case/sim/c3sim) importable
        parts += [str(tree / "src"), str(tree / "sim")]
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


@dataclass
class ReplayResult:
    ok: bool
    error: str | None = None


@dataclass
class CheckResult:
    violations: list = field(default_factory=list)
    error: str | None = None


def replay(tree: Path, scenario: str, seed: int, out: Path, faults: Path | None = None) -> ReplayResult:
    out.mkdir(parents=True, exist_ok=True)
    argv = [grader_python(), "-m", "sim", "replay", "--scenario", scenario, "--seed", str(seed),
            "--out", str(out)]
    if faults:
        argv += ["--faults", str(faults)]
    try:
        r = subprocess.run(argv, cwd=tree, env=clean_env(tree, with_src=False), capture_output=True,
                           text=True, timeout=REPLAY_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return ReplayResult(False, f"replay timed out after {REPLAY_TIMEOUT_S:.0f}s wall")
    (out / "replay.stderr").write_text(r.stdout[-20000:] + r.stderr[-20000:])
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        return ReplayResult(False, f"exit {r.returncode}: {' | '.join(tail)[:300]}")
    if not (out / "trace.jsonl").exists():
        return ReplayResult(False, "replay wrote no trace.jsonl")
    return ReplayResult(True)


def check(tree: Path, run_dir: Path, reference_messages: int | None = None) -> CheckResult:
    argv = [grader_python(), "-m", "sim", "check", "--trace", str(run_dir / "trace.jsonl")]
    if reference_messages is not None and _check_supports_reference(tree):
        argv += ["--reference-messages", str(reference_messages)]
    try:
        r = subprocess.run(argv, cwd=tree, env=clean_env(tree, with_src=False), capture_output=True,
                           text=True, timeout=CHECK_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return CheckResult(error="checker timed out")
    summ = load_summary(run_dir)
    if "invariants" in summ:
        return CheckResult(list(summ["invariants"] or []))
    # Fall back to JSON on stdout.
    try:
        data = json.loads(r.stdout)
        if isinstance(data, dict) and "invariants" in data:
            return CheckResult(list(data["invariants"] or []))
        if isinstance(data, list):
            return CheckResult(data)
    except ValueError:
        pass
    if r.returncode not in (0, 1):
        return CheckResult(error=f"exit {r.returncode}: {(r.stderr or r.stdout).strip()[-300:]}")
    return CheckResult(error="checker produced no invariants result")


_ref_support: dict[str, bool] = {}


def _check_supports_reference(tree: Path) -> bool:
    key = str(tree)
    if key not in _ref_support:
        try:
            r = subprocess.run([grader_python(), "-m", "sim", "check", "--help"], cwd=tree,
                               env=clean_env(tree, with_src=False), capture_output=True, text=True, timeout=30)
            _ref_support[key] = "--reference-messages" in r.stdout
        except subprocess.TimeoutExpired:
            _ref_support[key] = False
    return _ref_support[key]


def load_trace(path: Path) -> list[dict]:
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_summary(run_dir: Path) -> dict:
    p = run_dir / "summary.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except ValueError:
        return {}


def count_messages(trace: Path) -> int:
    n = 0
    with open(trace, "rb") as f:
        for line in f:
            if b'"msg_send"' in line and json.loads(line).get("kind") == "msg_send":
                n += 1
    return n
