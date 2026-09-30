#!/usr/bin/env python3
"""Proxy forwarder for fixer containers (clarifications C8).

Used by grade.py as a sidecar: `python3 entry.py --serve` listens on
127.0.0.1:8787 and forwards to /run/c3-proxy.sock; the fixer container joins the
sidecar's network namespace, so it runs its own entrypoint unchanged and needs
no Python. `--probe` exits 0 once the forwarder accepts connections.

The older wrapper mode is kept below:

The container runs with --network none. The grader bind-mounts the proxy's Unix
socket at /run/c3-proxy.sock and this file at /c3/entry.py, and sets
--entrypoint python3 with args: /c3/entry.py -- <original entrypoint + cmd>.

We fork a TCP forwarder on 127.0.0.1:8787 -> /run/c3-proxy.sock (B3's
/c3/forwarder.py when mounted, else the built-in one below), wait until it
accepts connections, then exec the fixer so it becomes PID 1 and receives the
grader's SIGTERM directly.
"""
import os
import socket
import sys
import threading
import time

SOCK = os.environ.get("C3_PROXY_SOCK_IN", "/run/c3-proxy.sock")
HOST, PORT = "127.0.0.1", 8787


def pipe(a, b):
    try:
        while True:
            d = a.recv(65536)
            if not d:
                break
            b.sendall(d)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(64)
    while True:
        c, _ = srv.accept()
        u = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            u.connect(SOCK)
        except OSError:
            c.close()
            continue
        for x, y in ((c, u), (u, c)):
            threading.Thread(target=pipe, args=(x, y), daemon=True).start()


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--serve"]:          # sidecar mode: just the forwarder
        b3 = "/c3/forwarder.py"
        if os.path.exists(b3) and os.environ.get("C3_B3_FORWARDER") == "1":
            os.execvp(sys.executable, [sys.executable, b3, "--listen", f"{HOST}:{PORT}", "--unix", SOCK])
        serve()
        return
    if argv[:1] == ["--probe"]:
        socket.create_connection((HOST, PORT), timeout=0.5).close()
        return
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("docker_entry: no fixer command", file=sys.stderr)
        sys.exit(64)
    pid = os.fork()
    if pid == 0:
        os.setsid()
        b3 = "/c3/forwarder.py"
        if os.path.exists(b3) and os.environ.get("C3_B3_FORWARDER") == "1":
            os.execvp(sys.executable, [sys.executable, b3, "--listen", f"{HOST}:{PORT}", "--unix", SOCK])
        serve()
        os._exit(0)
    for _ in range(100):
        try:
            socket.create_connection((HOST, PORT), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        print("docker_entry: forwarder did not start", file=sys.stderr)
    os.execvp(argv[0], argv)


if __name__ == "__main__":
    main()
