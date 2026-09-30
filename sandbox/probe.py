#!/usr/bin/env python3
"""T5 sandbox probe. Runs INSIDE the sandbox and prints one JSON object to stdout.

    python3 /opt/c3/probe.py --session NAME [--kind run|designer]
        [--forbid PATH ...] [--tcp HOST:PORT ...] [--no-require-proxy]

It tries a fixed list of forbidden paths, container sockets, host sockets and
outbound connections. It passes only if every one of them is denied, the
proxy's admin API is refused, and (unless --no-require-proxy) the model proxy
itself is reachable at 127.0.0.1:8787. It also checks that no Anthropic
credentials (credentials file, OAuth token) are present: designers build through
the proxy with a designer-build token (clarification C14).

sandbox/run.sh --probe NAME and sandbox/designer.sh run this at session start and
write the JSON to evidence/tests/T5-<NAME>.json.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import socket
import sys
import urllib.parse

# Host paths as they exist OUTSIDE the sandbox. None of them may be readable inside.
AV2 = "/home/sam/personal_projects/c3_workspace/hiring/assessment-v2"
TA = "/home/sam/personal_projects/c3_workspace/hiring/technical-assessment"
FORBIDDEN = [
    "/home/sam", "/home/sam/.ssh", "/home/sam/.claude", "/home/sam/.claude/.credentials.json",
    "/home/sam/.config", "/home/sam/.aws", "/home/sam/.docker", "/home/sam/personal_projects",
    AV2, AV2 + "/.env", AV2 + "/private", AV2 + "/cases/hidden", AV2 + "/evidence",
    AV2 + "/evidence/ledger.md", AV2 + "/evidence/spend.jsonl", AV2 + "/contracts",
    AV2 + "/proxy/.state", AV2 + "/proxy/.state/admin.token", AV2 + "/codebases", AV2 + "/grader",
    TA, TA + "/docs/simple-v2-build-plan.md", TA + "/docs/simple-v2-design.md",
    "/root", "/etc/shadow", "/etc/sudoers", "/var/lib/docker", "/run/user/1000",
]
CONTAINER_SOCKETS = [
    "/var/run/docker.sock", "/run/docker.sock", "/run/containerd/containerd.sock",
    "/run/user/1000/docker.sock", "/run/podman/podman.sock", "/run/user/1000/podman/podman.sock",
    "/run/dbus/system_bus_socket", "/run/user/1000/bus", "/tmp/.X11-unix/X0",
    "/run/systemd/private", "/run/user/1000/systemd/private",
]
ABSTRACT_SOCKETS = ["/tmp/.X11-unix/X0", "/tmp/dbus-session"]
OUTBOUND_TCP = [("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443), ("140.82.112.3", 443),
                ("2606:4700:4700::1111", 443), ("openrouter.ai", 443), ("api.anthropic.com", 443),
                ("github.com", 443), ("pypi.org", 443), ("10.0.0.1", 22), ("172.17.0.1", 2375),
                ("192.168.1.1", 80)]
EGRESS_DENY = ["github.com:443", "openrouter.ai:443", "1.1.1.1:443", "api.anthropic.com:80",
               "evil.api.anthropic.com.attacker.net:443", "127.0.0.1:22"]


def check(results, name, target, expect, ok, detail=""):
    results.append({"name": name, "target": target, "expect": expect, "ok": bool(ok), "detail": detail})


def readable(p):
    """True if the path leaks content: a readable file, or a non-empty listable dir."""
    try:
        if os.path.isdir(p):
            return len(os.listdir(p)) > 0
        with open(p, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


def tcp_connect(host, port, timeout=3.0):
    try:
        c = socket.create_connection((host, port), timeout=timeout)
        c.close()
        return True, "connected"
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def http(host, port, method, path, headers=None, timeout=5.0):
    """Minimal HTTP/1.0 client. Returns (status, body) or (None, error)."""
    try:
        c = socket.create_connection((host, port), timeout=timeout)
        h = "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items())
        c.sendall(f"{method} {path} HTTP/1.0\r\nHost: {host}\r\n{h}Content-Length: 0\r\n\r\n".encode())
        data = b""
        while True:
            b = c.recv(65536)
            if not b:
                break
            data += b
        c.close()
        line = data.split(b"\r\n", 1)[0].decode("latin1")
        return int(line.split()[1]), data.split(b"\r\n\r\n", 1)[-1][:300].decode("latin1")
    except (OSError, ValueError, IndexError) as e:
        return None, f"{type(e).__name__}: {e}"


def connect_via(proxy_host, proxy_port, target, timeout=8.0):
    """Send CONNECT through an HTTP proxy. Returns (status or None, detail)."""
    try:
        c = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
        c.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        data = b""
        while b"\r\n" not in data:
            b = c.recv(4096)
            if not b:
                break
            data += b
        c.close()
        line = data.split(b"\r\n", 1)[0].decode("latin1")
        return int(line.split()[1]), line
    except (OSError, ValueError, IndexError) as e:
        return None, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="adhoc")
    ap.add_argument("--kind", default="run", choices=["run", "designer"])
    ap.add_argument("--forbid", action="append", default=[])
    ap.add_argument("--tcp", action="append", default=[], help="extra HOST:PORT that must be unreachable")
    ap.add_argument("--no-require-proxy", action="store_true")
    ap.add_argument("--proxy-port", type=int, default=8787)
    a = ap.parse_args()

    res = []
    # 1. Filesystem
    for p in FORBIDDEN + a.forbid:
        leak = readable(p)
        check(res, "forbidden_path", p, "denied", not leak,
              ("READABLE" if leak else ("exists-but-empty/unreadable" if os.path.lexists(p) else "absent")))
    for p in CONTAINER_SOCKETS:
        if not os.path.lexists(p):
            check(res, "host_socket", p, "denied", True, "absent")
            continue
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        try:
            s.connect(p)
            check(res, "host_socket", p, "denied", False, "CONNECTABLE")
        except OSError as e:
            check(res, "host_socket", p, "denied", True, f"present but {type(e).__name__}")
        finally:
            s.close()
    for name in ABSTRACT_SOCKETS:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        try:
            s.connect("\0" + name)
            check(res, "abstract_socket", "@" + name, "denied", False, "CONNECTABLE")
        except OSError as e:
            check(res, "abstract_socket", "@" + name, "denied", True, type(e).__name__)
        finally:
            s.close()
    for d in ("/usr", "/usr/bin", "/etc", "/opt"):
        p = os.path.join(d, ".c3probe")
        try:
            with open(p, "w") as f:
                f.write("x")
            os.unlink(p)
            check(res, "readonly_system", d, "denied", False, "WRITABLE")
        except OSError as e:
            check(res, "readonly_system", d, "denied", True, type(e).__name__)
    ro = os.environ.get("C3_CORPUS")
    if ro and os.path.isdir(ro):
        p = os.path.join(ro, ".c3probe")
        try:
            with open(p, "w") as f:
                f.write("x")
            os.unlink(p)
            check(res, "readonly_corpus", ro, "denied", False, "WRITABLE")
        except OSError as e:
            check(res, "readonly_corpus", ro, "denied", True, type(e).__name__)

    # 2. Environment
    bad = [k for k, v in os.environ.items() if v.startswith(("sk-or-", "sk-ant-")) or k == "DOCKER_HOST"]
    check(res, "env_secrets", "environment", "denied", not bad, ",".join(bad) or "none")

    # 3. Network
    extra = []
    for t in a.tcp:
        h, _, p = t.rpartition(":")
        extra.append((h.strip("[]"), int(p)))
    for h, p in OUTBOUND_TCP + extra:
        ok, detail = tcp_connect(h, p)
        check(res, "outbound_tcp", f"{h}:{p}", "denied", not ok, detail)
    try:
        socket.getaddrinfo("openrouter.ai", 443)
        check(res, "dns", "openrouter.ai", "denied", False, "RESOLVED")
    except OSError as e:
        check(res, "dns", "openrouter.ai", "denied", True, type(e).__name__)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(2)
    try:
        u.sendto(b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x06google\x03com\x00\x00\x01\x00\x01",
                 ("8.8.8.8", 53))
        u.recvfrom(512)
        check(res, "outbound_udp", "8.8.8.8:53", "denied", False, "GOT REPLY")
    except OSError as e:
        check(res, "outbound_udp", "8.8.8.8:53", "denied", True, type(e).__name__)
    finally:
        u.close()
    ifaces = sorted(n for _, n in socket.if_nameindex())
    check(res, "interfaces", ",".join(ifaces), "denied", ifaces == ["lo"], "only loopback expected")

    # 4. Model proxy: reachable, admin refused
    st, body = http("127.0.0.1", a.proxy_port, "GET", "/healthz")
    proxy_ok = st == 200
    check(res, "proxy_reachable", f"127.0.0.1:{a.proxy_port}/healthz", "allowed",
          proxy_ok or a.no_require_proxy, f"status={st} {'' if proxy_ok else body[:120]}")
    if proxy_ok:
        st, body = http("127.0.0.1", a.proxy_port, "GET", "/admin/tokens")
        check(res, "proxy_admin", "/admin/tokens via sandbox socket", "denied", st in (401, 403, 404),
              f"status={st}")
        st, body = http("127.0.0.1", a.proxy_port, "POST", "/admin/tokens")
        check(res, "proxy_admin", "POST /admin/tokens via sandbox socket", "denied", st in (401, 403, 404),
              f"status={st}")

    # 5. No egress proxy and no Anthropic credentials (C14: designers build through our proxy)
    hp = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if hp:
        pu = urllib.parse.urlsplit(hp)
        for tgt in EGRESS_DENY + ["api.anthropic.com:443"]:
            st, line = connect_via(pu.hostname, pu.port or 80, tgt)
            check(res, "egress_denied", tgt, "denied", st != 200, line)
    home = os.environ.get("HOME", "/home/candidate")
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", os.path.join(home, ".claude"))
    for p in {os.path.join(cfg, ".credentials.json"), os.path.join(home, ".claude", ".credentials.json")}:
        check(res, "anthropic_credentials", p, "denied", not os.path.exists(p),
              "PRESENT" if os.path.exists(p) else "absent")
    oauth = [k for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN") if os.environ.get(k)]
    check(res, "anthropic_credentials", "environment", "denied", not oauth, ",".join(oauth) or "none")

    passed = all(r["ok"] for r in res)
    out = {"test": "T5", "session": a.session, "kind": a.kind,
           "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
           "passed": passed, "failures": [r for r in res if not r["ok"]],
           "n_checks": len(res), "checks": res}
    print(json.dumps(out, indent=1))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
