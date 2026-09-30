"""Shared helpers for the grader: paths, YAML/JSON loading, patch handling.

Standard library plus PyYAML (already a runtime dependency for fault configs).
"""
from __future__ import annotations

import difflib
import re
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GRADER = Path(__file__).resolve().parent
# assessment-v2/ (or the pack root). C3_ASSESSMENT_ROOT points the grader at a
# mirror tree (grader/stubs/make_root.py) for self-tests.
ROOT = Path(os.environ["C3_ASSESSMENT_ROOT"]).resolve() if os.environ.get("C3_ASSESSMENT_ROOT") \
    else GRADER.parent

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

CASE_SPLITS = ("practice", "hidden")
PRACTICE_CODEBASES = ("A", "B")
IGNORED_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
IGNORED_SUFFIXES = (".pyc", ".pyo")


class GraderError(Exception):
    """A case or configuration problem (not a fixer failure)."""


# ---------------------------------------------------------------- loading

def load_yaml(path: Path):
    if yaml is None:
        raise GraderError("PyYAML is required (pip install pyyaml)")
    with open(path) as f:
        return yaml.safe_load(f)


def load_json(path: Path):
    return json.loads(Path(path).read_text())


def write_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n")
    os.replace(tmp, path)


def grader_python() -> str:
    """Python used for tests and replays: needs pytest and PyYAML."""
    env = os.environ.get("C3_GRADER_PYTHON")
    if env:
        return env
    venv = GRADER / ".venv" / "bin" / "python"
    if venv.exists():
        return str(venv)
    return sys.executable


# ---------------------------------------------------------------- case lookup

def find_case_dirs(spec: str, cases_root: Path | None = None) -> list[Path]:
    """Resolve --cases: a case dir, a dir of case dirs, or comma/space separated IDs."""
    cases_root = Path(cases_root) if cases_root else ROOT / "cases"
    out: list[Path] = []
    for item in [s for s in spec.replace(",", " ").split() if s]:
        p = Path(item)
        if p.is_dir() and (p / "case.json").exists():
            out.append(p.resolve())
        elif p.is_dir():
            subs = sorted(d for d in p.iterdir() if (d / "case.json").exists())
            if not subs:  # maybe a split dir of split dirs (cases/)
                subs = sorted(d for s in p.iterdir() if s.is_dir()
                              for d in s.iterdir() if (d / "case.json").exists())
            if not subs:
                raise GraderError(f"no cases (case.json) under {p}")
            out.extend(d.resolve() for d in subs)
        else:
            hits = [cases_root / s / item for s in CASE_SPLITS
                    if (cases_root / s / item / "case.json").exists()]
            if not hits and (cases_root / item / "case.json").exists():
                hits = [cases_root / item]
            if not hits:
                raise GraderError(f"case {item!r} not found under {cases_root}")
            out.append(hits[0].resolve())
    seen, uniq = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def find_private_dir(case_id: str, private_root: Path | None, case_dir: Path | None = None) -> Path:
    """Private grading material for a case.

    Order: --private-root/<ID>, ROOT/private/<ID>, then next to the cases tree:
    <cases>/../private/<ID> and <cases>/../grading/<ID> (pack layout).
    """
    cands = []
    if private_root:
        cands.append(Path(private_root) / case_id)
    cands.append(ROOT / "private" / case_id)
    if case_dir is not None:
        for up in (Path(case_dir).parent.parent, Path(case_dir).parent):
            cands += [up / "private" / case_id, up / "grading" / case_id]
    for c in cands:
        if (c / "incident.yaml").exists():
            return c.resolve()
    raise GraderError(f"no private material (incident.yaml) for case {case_id}; tried "
                      + ", ".join(str(c) for c in cands))


def find_regression_dir(private_dir: Path, codebase: str, codebases_root: Path | None) -> Path | None:
    if (private_dir / "regression").is_dir():
        return private_dir / "regression"
    roots = [Path(codebases_root)] if codebases_root else []
    roots.append(ROOT / "codebases")
    for r in roots:
        if (r / codebase / "regression").is_dir():
            return r / codebase / "regression"
    return None


# ---------------------------------------------------------------- file trees

def _ignore(dirpath, names):
    return [n for n in names if n in IGNORED_NAMES or n.endswith(IGNORED_SUFFIXES)]


def copy_tree(src: Path, dst: Path, *, follow_symlinks: bool = False) -> list[str]:
    """Copy a tree, dropping caches and bytecode. Symlinks are dropped (not
    followed) unless follow_symlinks; returns the list of dropped symlinks."""
    src, dst = Path(src), Path(dst)
    dropped: list[str] = []
    dst.mkdir(parents=True, exist_ok=True)
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        rel = Path(dirpath).relative_to(src)
        keep = []
        for d in sorted(dirnames):
            full = Path(dirpath) / d
            if d in IGNORED_NAMES:
                continue
            if full.is_symlink():
                dropped.append(str(rel / d))
                continue
            keep.append(d)
            (dst / rel / d).mkdir(parents=True, exist_ok=True)
        dirnames[:] = keep
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            if fn.endswith(IGNORED_SUFFIXES):
                continue
            if full.is_symlink() and not follow_symlinks:
                dropped.append(str(rel / fn))
                continue
            if not full.is_file():
                continue
            shutil.copy2(full, dst / rel / fn)
    return dropped


def tree_files(root: Path) -> dict[str, bytes]:
    root = Path(root)
    out: dict[str, bytes] = {}
    if not root.exists():
        return out
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_NAMES
                             and not (Path(dirpath) / d).is_symlink())
        for fn in sorted(filenames):
            full = Path(dirpath) / fn
            if fn.endswith(IGNORED_SUFFIXES) or full.is_symlink() or not full.is_file():
                continue
            out[full.relative_to(root).as_posix()] = full.read_bytes()
    return out


def diff_trees(old: Path, new: Path, prefix: str = "src") -> tuple[str, list[str]]:
    """Canonical unified diff old->new (paths prefixed with `prefix/`) and the
    sorted list of changed files. Deterministic, so the same patch hashes the same."""
    a, b = tree_files(old), tree_files(new)
    changed = sorted(p for p in set(a) | set(b) if a.get(p) != b.get(p))
    chunks: list[str] = []
    for p in changed:
        name = f"{prefix}/{p}" if prefix else p
        old_b, new_b = a.get(p), b.get(p)
        try:
            ol = old_b.decode().splitlines(keepends=True) if old_b is not None else []
            nl = new_b.decode().splitlines(keepends=True) if new_b is not None else []
        except UnicodeDecodeError:
            chunks.append(f"Binary files a/{name} and b/{name} differ "
                          f"({hashlib.sha256(old_b or b'').hexdigest()[:12]} -> "
                          f"{hashlib.sha256(new_b or b'').hexdigest()[:12]})\n")
            continue
        fa = f"a/{name}" if old_b is not None else "/dev/null"
        fb = f"b/{name}" if new_b is not None else "/dev/null"
        lines = list(difflib.unified_diff(ol, nl, fa, fb))
        chunks.append(f"diff --git a/{name} b/{name}\n")
        for ln in lines:
            chunks.append(ln if ln.endswith("\n") else ln + "\n\\ No newline at end of file\n")
    names = [f"{prefix}/{p}" if prefix else p for p in changed]
    return "".join(chunks), names


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def apply_patch(root: Path, patch: Path | str, *, reverse: bool = False) -> None:
    """Apply a unified diff to the tree at `root` (which contains src/).

    Accepts patches rooted at the case (src/...), at src/ itself, with or
    without a/ b/ prefixes. Uses `git apply` (works outside a repository),
    falling back to `patch`.
    """
    root = Path(root)
    text = Path(patch).read_text() if isinstance(patch, Path) else patch
    if not text.strip():
        return
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(text)
        pf = f.name
    try:
        attempts = []
        for cwd in (root, root / "src"):
            if not cwd.is_dir():
                continue
            for p in (1, 0):
                args = ["git", "apply", f"-p{p}", "--whitespace=nowarn"]
                if reverse:
                    args.append("-R")
                chk = subprocess.run(args + ["--check", pf], cwd=cwd, capture_output=True,
                                     text=True, env=_git_env(cwd))
                if chk.returncode == 0:
                    before = tree_files(root / "src")
                    subprocess.run(args + [pf], cwd=cwd, check=True, capture_output=True,
                                   text=True, env=_git_env(cwd))
                    if tree_files(root / "src") != before:
                        return
                    attempts.append(f"git apply -p{p} in {cwd.name}/: applied nothing")
                attempts.append(f"git apply -p{p} in {cwd.name}/: {chk.stderr.strip()[:300]}")
        for cwd in (root, root / "src"):
            if not cwd.is_dir():
                continue
            for p in (1, 0):
                args = ["patch", f"-p{p}", "--batch", "--forward", "-s"]
                if reverse:
                    args.append("-R")
                chk = subprocess.run(args + ["--dry-run", "-i", pf], cwd=cwd, capture_output=True, text=True)
                if chk.returncode == 0:
                    subprocess.run(args + ["-i", pf], cwd=cwd, check=True, capture_output=True, text=True)
                    return
                attempts.append(f"patch -p{p} in {cwd.name}/: {(chk.stdout + chk.stderr).strip()[:300]}")
        raise GraderError(f"patch {patch if isinstance(patch, Path) else '<text>'} does not apply to {root}:\n  "
                          + "\n  ".join(attempts))
    finally:
        os.unlink(pf)


def _git_env(cwd: Path | None = None):
    env = dict(os.environ)
    # Make sure git never walks up into an enclosing repository: inside one, `git apply`
    # silently skips every path outside the current directory and exits 0.
    env["GIT_CEILING_DIRECTORIES"] = str(Path(cwd).resolve().parent) if cwd else "/"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def split_patch(text: str) -> list[tuple[str, str]]:
    """Split a unified diff into (file_path, file_patch_text) chunks, one per file.

    Hunk bodies are consumed by their line counts, so removed lines that look
    like headers are handled. Paths are normalised relative to the case root."""
    lines = text.splitlines(keepends=True)
    out: list[tuple[str, str]] = []
    cur: list[str] = []
    path: str | None = None
    hunk_re = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")

    def flush():
        nonlocal cur, path
        if cur and any(l.startswith("@@") or l.startswith("Binary") for l in cur):
            out.append((path or "?", "".join(cur)))
        cur, path = [], None

    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            flush()
            cur.append(ln)
        elif ln.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if any(l.startswith("@@") for l in cur):
                flush()
            cur.append(ln)
            cur.append(lines[i + 1])
            for hdr in (ln, lines[i + 1]):
                name = hdr[4:].split("\t")[0].strip()
                if name != "/dev/null":
                    path = _norm_path(name)
            i += 1
        elif (m := hunk_re.match(ln)):
            cur.append(ln)
            old_n = int(m.group(1)) if m.group(1) is not None else 1
            new_n = int(m.group(2)) if m.group(2) is not None else 1
            while (old_n > 0 or new_n > 0) and i + 1 < len(lines):
                i += 1
                body = lines[i]
                cur.append(body)
                if body.startswith("\\"):
                    continue
                tag = body[:1]
                if tag == "-":
                    old_n -= 1
                elif tag == "+":
                    new_n -= 1
                else:
                    old_n -= 1
                    new_n -= 1
            while i + 1 < len(lines) and lines[i + 1].startswith("\\"):
                i += 1
                cur.append(lines[i])
        else:
            cur.append(ln)
        i += 1
    flush()
    return out


def _norm_path(name: str) -> str:
    for pre in ("a/", "b/"):
        if name.startswith(pre):
            name = name[2:]
            break
    if not name.startswith("src/"):
        name = "src/" + name
    return name


def rmtree(path: Path) -> None:
    def onerror(func, p, exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=onerror)
