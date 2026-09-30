"""Run one fixer on one workspace, in process mode or docker mode.

Process mode fixer directory layout (either):
    fixer.json   {"id": "noop", "command": ["python3", "fix.py"]}   # argv, run with cwd=workspace;
                 relative argv entries that name files in the fixer dir are resolved
    run          executable, run with cwd=workspace

Kill policy (fixer.md): SIGTERM at the deadline, SIGKILL `kill_grace` seconds later.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from common import ROOT, GraderError

MODEL = "anthropic/claude-sonnet-4.6"
MAX_IMAGE_BYTES = 4 * 1024 ** 3


class PlatformError(GraderError):
    """Candidate error: the image is not linux/amd64."""

    def __init__(self, arch: str, msg: str):
        super().__init__(msg)
        self.arch = arch


class FixerInfraError(GraderError):
    """Sandbox/docker failure: the run is an infrastructure error, not scored."""


@dataclass
class FixerSpec:
    kind: str            # "dir" | "image"
    ref: str             # fixer dir path or image name
    id: str
    command: list[str] | None = None


@dataclass
class FixerOutcome:
    exit_code: int | None      # negative = killed by that signal
    timed_out: bool
    wall_s: float
    killed: bool
    log_path: Path


def resolve_fixer(ref: str, mode: str) -> FixerSpec:
    p = Path(ref)
    if mode == "process" or (p.exists() and p.is_dir()):
        if not p.is_dir():
            raise GraderError(f"fixer dir {ref} not found (process mode needs a fixer directory)")
        p = p.resolve()
        fid = p.name
        cmd = None
        if (p / "run.sh").exists():                 # 
            cmd = [str(p / "run.sh")] if os.access(p / "run.sh", os.X_OK) else ["/bin/sh", str(p / "run.sh")]
        elif (p / "fixer.json").exists():
            spec = json.loads((p / "fixer.json").read_text())
            cmd = spec.get("command")
            if isinstance(cmd, str):
                cmd = ["/bin/sh", "-c", cmd]
            cmd = [str(p / c) if (p / c).exists() and not c.startswith("-") else c for c in cmd]
        elif (p / "run").exists():
            cmd = [str(p / "run")]
        if (p / "fixer.json").exists():
            fid = json.loads((p / "fixer.json").read_text()).get("id", fid)
        if not cmd:
            raise GraderError(f"fixer dir {p} has no fixer.json command or executable 'run'")
        return FixerSpec("dir", str(p), fid, cmd)
    return FixerSpec("image", ref, ref.replace("/", "_").replace(":", "_"))


def base_env(*, workspace: str, corpus: str, deadline: int, budget_usd: float,
             proxy_env: dict[str, str], home: str | None) -> dict[str, str]:
    env = {
        "C3_WORKSPACE": workspace,
        "C3_CORPUS": corpus,
        "C3_DEADLINE": str(deadline),
        "C3_BUDGET_USD": f"{budget_usd:.2f}",
        "ANTHROPIC_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if home:
        env["HOME"] = home
    env.update(proxy_env)
    return env


def _kill_group(proc: subprocess.Popen, sig) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _wait_with_deadline(proc, deadline_epoch: float, grace: float, on_term, on_kill):
    timed_out = killed = False
    while True:
        remaining = deadline_epoch - time.time()
        try:
            proc.wait(timeout=max(0.0, min(remaining, 1.0)) if remaining > 0 else 0.01)
            break
        except subprocess.TimeoutExpired:
            if time.time() >= deadline_epoch:
                timed_out = True
                on_term()
                try:
                    proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    killed = True
                    on_kill()
                    proc.wait()
                break
    return timed_out, killed


# ---------------------------------------------------------------- process mode

def sandbox_script() -> Path | None:
    p = ROOT / "sandbox" / "run.sh"
    return p if p.exists() else None


def run_process(spec: FixerSpec, *, workspace: Path, corpus: Path, budget_s: float,
                budget_usd: float, proxy_env: dict[str, str], scratch: Path,
                log_path: Path, sandbox: str = "auto", grace: float = 5.0) -> FixerOutcome:
    """sandbox: 'on' (require sandbox/run.sh), 'off' (plain subprocess), 'auto'."""
    sb = sandbox_script() if sandbox != "off" else None
    if sandbox == "on" and sb is None:
        raise FixerInfraError("sandbox requested but sandbox/run.sh does not exist")
    home = scratch / "home"
    home.mkdir(parents=True, exist_ok=True)
    start = time.time()
    deadline = int(start + budget_s)
    if sb:
        # run.sh sets the fixer environment itself.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"}
        argv = [str(sb), "--workspace", str(workspace), "--corpus", str(corpus),
                "--token", proxy_env["OPENROUTER_API_KEY"], "--budget", f"{budget_usd:.2f}",
                "--deadline", str(deadline), "--fixer", spec.ref, "--home", str(home)]
        sock = proxy_socket()
        if sock.exists():
            argv += ["--proxy-sock", str(sock)]
        # sandbox/run.sh mounts the fixer directory read-only at /fixer
        inside = [("/fixer" + c[len(spec.ref):]) if c.startswith(spec.ref) else c for c in spec.command]
        argv += ["--"] + inside
        cwd = str(workspace)
    else:
        env = base_env(workspace=str(workspace), corpus=str(corpus), deadline=deadline,
                       budget_usd=budget_usd, proxy_env=proxy_env, home=str(home))
        env["PATH"] = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        argv = spec.command
        cwd = str(workspace)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "wb") as log:
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            raise FixerInfraError(f"could not start fixer: {e}") from e
        timed_out, killed = _wait_with_deadline(
            proc, deadline, grace,
            lambda: _kill_group(proc, signal.SIGTERM),
            lambda: _kill_group(proc, signal.SIGKILL))
        # Kill any stragglers left in the group after a normal exit.
        _kill_group(proc, signal.SIGKILL)
    wall = time.time() - start
    return FixerOutcome(proc.returncode, timed_out, round(wall, 3), killed, log_path)


# ---------------------------------------------------------------- docker mode

def docker_image_check(image: str) -> None:
    """Reject missing, non-amd64 and oversized images with a clear message."""
    r = subprocess.run(["docker", "image", "inspect", image, "--format",
                        "{{.Os}}/{{.Architecture}} {{.Size}}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise GraderError(f"docker image {image!r} not found locally (docker pull or docker load it first): "
                          f"{r.stderr.strip()[:200]}")
    platform, size = r.stdout.strip().split()
    if platform != "linux/amd64":
        raise PlatformError(platform.split("/")[-1], f"REJECTED: image {image!r} is {platform}; fixer images must be linux/amd64 "
                          f"(build with: docker build --platform linux/amd64 ...)")
    if int(size) > MAX_IMAGE_BYTES:
        raise GraderError(f"REJECTED: image {image!r} is {int(size)/1e9:.2f} GB; the limit is 4 GB")


from proxy_client import proxy_socket  # noqa: E402


def image_command(image: str) -> list[str]:
    r = subprocess.run(["docker", "image", "inspect", image, "--format",
                        "{{json .Config.Entrypoint}}|{{json .Config.Cmd}}"], capture_output=True, text=True)
    if r.returncode != 0:
        raise GraderError(f"docker image inspect {image}: {r.stderr.strip()[:200]}")
    ep, cmd = r.stdout.strip().split("|", 1)
    argv = (json.loads(ep) or []) + (json.loads(cmd) or [])
    if not argv:
        raise GraderError(f"image {image} has no ENTRYPOINT or CMD")
    return argv


FORWARDER_IMAGE_TAG = "c3-proxy-forwarder:latest"


def forwarder_image() -> str:
    """Image for the proxy sidecar: needs only python3."""
    env = os.environ.get("C3_FORWARDER_IMAGE")
    cands = [env] if env else []
    cands += [FORWARDER_IMAGE_TAG, "python:3.12.12-slim-bookworm", "python:3.12-slim",
              "c3-fixer-naive-mini:latest", "c3-fixer-naive-claude:latest"]
    for img in cands:
        r = subprocess.run(["docker", "image", "inspect", img], capture_output=True)
        if r.returncode == 0:
            return img
    ctx = Path(__file__).resolve().parent / "forwarder-image"
    r = subprocess.run(["docker", "build", "--platform", "linux/amd64", "-t", FORWARDER_IMAGE_TAG, str(ctx)],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        raise FixerInfraError(f"no python image for the proxy sidecar and building {FORWARDER_IMAGE_TAG} "
                              f"failed: {r.stderr[-300:]}")
    return FORWARDER_IMAGE_TAG


def start_sidecar(name: str, sock: Path) -> None:
    """Sidecar container with no network except loopback, running a TCP forwarder
    127.0.0.1:8787 -> the proxy's Unix socket. The fixer joins its network
    namespace (--network container:<sidecar>), so the fixer image needs nothing."""
    entry = Path(__file__).resolve().parent / "docker_entry.py"
    b3 = ROOT / "proxy" / "forwarder.py"      # B3's forwarder; ours is the fallback
    argv = ["docker", "run", "-d", "--rm", "--name", name, "--network", "none",
            "-v", f"{sock}:/run/c3-proxy.sock", "-v", f"{entry}:/c3/entry.py:ro"]
    if b3.exists():
        argv += ["-v", f"{b3}:/c3/forwarder.py:ro", "--entrypoint", "python3", forwarder_image(),
                 "/c3/forwarder.py", "127.0.0.1:8787=/run/c3-proxy.sock"]
    else:
        argv += ["--entrypoint", "python3", forwarder_image(), "/c3/entry.py", "--serve"]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise FixerInfraError(f"proxy sidecar failed to start: {r.stderr.strip()[-300:]}")
    for _ in range(100):
        chk = subprocess.run(["docker", "exec", name, "python3", "/c3/entry.py", "--probe"],
                             capture_output=True, timeout=30)
        if chk.returncode == 0:
            return
        time.sleep(0.1)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    raise FixerInfraError("proxy sidecar forwarder never came up")


def run_docker(spec: FixerSpec, *, workspace: Path, corpus: Path, budget_s: float,
               budget_usd: float, proxy_env: dict[str, str], scratch: Path,
               log_path: Path, grace: float = 5.0) -> FixerOutcome:
    """the image's own entrypoint, no network except the
    proxy. The proxy's Unix socket is bind-mounted into a sidecar that forwards
    127.0.0.1:8787 to it; the fixer shares the sidecar's network namespace."""
    sock = proxy_socket()
    stub_proxy = proxy_env.get("OPENROUTER_API_KEY", "").startswith("stub-")
    if not sock.exists() and not stub_proxy:
        raise FixerInfraError(f"proxy Unix socket {sock} missing (is proxy/ running?)")
    uid = uuid.uuid4().hex[:12]
    name, side = f"c3fix-{uid}", f"c3fwd-{uid}"
    if sock.exists():
        start_sidecar(side, sock)
        net = f"container:{side}"
    else:
        net = "none"
    start = time.time()
    deadline = int(start + budget_s)
    env = base_env(workspace="/workspace", corpus="/corpus", deadline=deadline,
                   budget_usd=budget_usd, proxy_env=proxy_env, home=None)
    argv = ["docker", "run", "--rm", "--init", "--name", name, "--platform", "linux/amd64",
            "--cpus", "2", "--memory", "4g", "--network", net,
            "-v", f"{workspace}:/workspace", "-v", f"{corpus}:/corpus:ro"]
    for k, v in sorted(env.items()):
        argv += ["-e", f"{k}={v}"]
    argv.append(spec.ref)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(log_path, "wb") as log:
            proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    start_new_session=True)

            def term():
                subprocess.run(["docker", "kill", "--signal", "TERM", name], capture_output=True)

            def kill():
                subprocess.run(["docker", "kill", name], capture_output=True)
                _kill_group(proc, signal.SIGKILL)

            timed_out, killed = _wait_with_deadline(proc, deadline, grace, term, kill)
    finally:
        if net != "none":
            subprocess.run(["docker", "rm", "-f", side], capture_output=True)
    wall = time.time() - start
    code = proc.returncode
    if code == 125 and not timed_out:
        raise FixerInfraError(f"docker run failed (exit 125): {log_path.read_text(errors='replace')[-300:]}")
    _docker_chown(spec.ref, workspace)
    return FixerOutcome(code, timed_out, round(wall, 3), killed, log_path)


def _docker_chown(image: str, workspace: Path) -> None:
    """Files written by a root container can't be removed by us; hand them back."""
    try:
        if all(os.stat(p).st_uid == os.getuid() for p in [workspace, *workspace.rglob("*")]):
            return
    except OSError:
        pass
    uid, gid = os.getuid(), os.getgid()
    for img in (image, "busybox", "alpine"):
        r = subprocess.run(["docker", "run", "--rm", "--network", "none", "--entrypoint", "chown",
                            "-v", f"{workspace}:/w", img, "-R", f"{uid}:{gid}", "/w"],
                           capture_output=True)
        if r.returncode == 0:
            return


def docker_available() -> bool:
    return shutil.which("docker") is not None
