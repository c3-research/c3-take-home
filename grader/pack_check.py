#!/usr/bin/env python3
"""T26 pack install check.

    python grader/pack_check.py [--pack DIR] [--image IMAGE] [--network host|none] [--no-docker]

1. The candidate pack (default: $C3_PACK_DIR, else pack/ or dist/pack/ under the
   repo root) is at most pack.max_bytes (thresholds.yaml, 1 GiB), counting every
   file (and --archive, if given).
2. Its README.md has a quick-start block: the first ```sh / ```bash / ```console
   fenced block after a heading containing "quick start" (case-insensitive), or
   any block tagged ```sh c3-quickstart. Lines starting with "$ " are commands in
   console blocks; other lines are commands in sh/bash blocks.
3. In a clean container (default image python:3.12.12-slim-bookworm, which has
   no pytest or PyYAML), the pack is copied to /tmp/pack and the quick-start
   commands run there, in order, with `set -e`. The check passes when they exit
   0 and at least one results/**/result.json exists with infra_error null.

Exit 0 on pass. Writes grader/.cache/checks/pack.json (and
$C3_EVIDENCE_DIR/pack-check.json when set).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GRADER, ROOT, load_yaml, write_json  # noqa: E402

DEFAULT_IMAGE = "python:3.12.12-slim-bookworm"


def find_pack(arg: str | None) -> Path | None:
    cands = [Path(arg)] if arg else []
    if os.environ.get("C3_PACK_DIR"):
        cands.append(Path(os.environ["C3_PACK_DIR"]))
    cands += [ROOT / "pack", ROOT / "dist" / "pack"]
    for c in cands:
        if (c / "README.md").exists():
            return c.resolve()
    return None


def tree_bytes(p: Path) -> int:
    n = 0
    for dirpath, _, files in os.walk(p, followlinks=False):
        for f in files:
            fp = Path(dirpath) / f
            if not fp.is_symlink():
                n += fp.stat().st_size
    return n


def quickstart(readme: str) -> list[str] | None:
    lines = readme.splitlines()
    fence = re.compile(r"^```\s*(\w+)?(.*)$")
    in_qs = False
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#"):
            in_qs = "quick start" in ln.lower() or "quickstart" in ln.lower()
        m = fence.match(ln)
        if m:
            lang, rest = (m.group(1) or ""), m.group(2)
            body = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                body.append(lines[i])
                i += 1
            if (in_qs and lang in ("sh", "bash", "console", "shell")) or "c3-quickstart" in rest:
                if lang == "console":
                    return [b[2:] for b in body if b.startswith("$ ")]
                return [b for b in body if b.strip() and not b.lstrip().startswith("#")]
        i += 1
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack")
    ap.add_argument("--archive", help="packed archive whose size is also checked")
    ap.add_argument("--image", default=os.environ.get("C3_PACK_IMAGE", DEFAULT_IMAGE))
    ap.add_argument("--network", default="host", help="container network (bridge is broken on this machine)")
    ap.add_argument("--no-docker", action="store_true", help="size and README checks only")
    ap.add_argument("--timeout", type=float, default=3600)
    a = ap.parse_args(argv)
    rep: dict = {"ran_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "passed": False, "checks": {}}

    def done(ok: bool, msg: str) -> int:
        rep["passed"] = ok
        rep["summary"] = msg
        write_json(GRADER / ".cache" / "checks" / "pack.json", rep)
        if os.environ.get("C3_EVIDENCE_DIR"):
            write_json(Path(os.environ["C3_EVIDENCE_DIR"]) / "pack-check.json", rep)
        print(f"{'PASS' if ok else 'FAIL'}  {msg}")
        return 0 if ok else 1

    pack = find_pack(a.pack)
    if pack is None:
        return done(False, "no candidate pack found (pass --pack, set C3_PACK_DIR, or build pack/)")
    rep["pack"] = str(pack)
    limit = 1 << 30
    try:
        limit = int(load_yaml(ROOT / "contracts" / "thresholds.yaml")["pack"]["max_bytes"])
    except Exception:
        pass
    size = tree_bytes(pack)
    rep["checks"]["size_bytes"] = size
    if a.archive:
        rep["checks"]["archive_bytes"] = Path(a.archive).stat().st_size
    if size > limit or rep["checks"].get("archive_bytes", 0) > limit:
        return done(False, f"pack is {size / 1e9:.2f} GB (limit {limit / 1e9:.2f} GB)")
    for bad in ("private", "evidence", "contracts", ".env"):
        if (pack / bad).exists():
            return done(False, f"pack contains {bad}/ (must never ship)")
    cmds = quickstart((pack / "README.md").read_text())
    if not cmds:
        return done(False, "README.md has no quick-start shell block")
    rep["checks"]["quickstart"] = cmds
    if a.no_docker:
        return done(True, f"pack {size / 1e6:.0f} MB, quick start found ({len(cmds)} commands); docker run skipped")
    script = "set -e\ncp -r /pack /tmp/pack\ncd /tmp/pack\n" + "\n".join(cmds) + "\n"
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out"
        out.mkdir()
        os.chmod(out, 0o777)
        script = script.replace("set -e\n", f"set -e\ntrap 'mkdir -p /out; cp -r /tmp/pack/results /out/ 2>/dev/null; "
                                f"chown -R {os.getuid()}:{os.getgid()} /out' EXIT\n", 1)
        argv = ["docker", "run", "--rm", "--network", a.network, "-v", f"{pack}:/pack:ro",
                "-v", f"{out}:/out", "--entrypoint", "/bin/sh", a.image, "-c", script]
        t0 = time.time()
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=a.timeout)
            code, log = r.returncode, r.stdout[-6000:] + r.stderr[-4000:]
        except subprocess.TimeoutExpired:
            code, log = -1, "timed out"
        rep["checks"]["container_exit"] = code
        rep["checks"]["container_seconds"] = round(time.time() - t0, 1)
        rep["checks"]["log_tail"] = log
        results = [json.loads(p.read_text()) for p in out.rglob("result.json")]
    rep["checks"]["results"] = len(results)
    if code != 0:
        return done(False, f"quick start failed in a clean {a.image} container (exit {code})")
    if not results or any(x.get("infra_error") for x in results):
        return done(False, f"quick start produced {len(results)} result.json, "
                           f"infra errors: {[x.get('infra_error') for x in results if x.get('infra_error')]}")
    return done(True, f"pack {size / 1e6:.0f} MB; quick start graded {len(results)} case(s) in a clean container")


if __name__ == "__main__":
    sys.exit(main())
