#!/usr/bin/env python3
"""TCP -> Unix socket forwarder, for sandboxes and --network none containers.

    python3 forwarder.py [127.0.0.1:8787=/run/c3-proxy.sock ...] [-- cmd args...]

Each mapping listens on HOST:PORT and relays every connection byte-for-byte to
the Unix socket. With no mapping, the default is 127.0.0.1:8787=/run/c3-proxy.sock.

- Without `-- cmd`, it runs in the foreground until killed.
- With `-- cmd`, it binds the listeners first (so they are ready before cmd
  starts), runs cmd as a child, forwards SIGTERM/SIGINT/SIGHUP to it, and exits
  with cmd's exit status. Use it as an entrypoint wrapper:
      docker run --network none -v SOCK:/run/c3-proxy.sock ... \
          --entrypoint python3 IMAGE /c3/forwarder.py -- /fixer/run.sh

Standard library only, Python 3.8+. Used by sandbox/run.sh, sandbox/designer.sh
and the grader's Docker mode.
"""
from __future__ import annotations

import os
import signal
import socket
import sys
import threading

DEFAULT = "127.0.0.1:8787=/run/c3-proxy.sock"


def _pipe(src: socket.socket, dst: socket.socket):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _handle(client: socket.socket, path: str):
    up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        up.connect(path)
    except OSError as e:
        sys.stderr.write(f"[forwarder] cannot reach {path}: {e}\n")
        client.close()
        up.close()
        return
    t = threading.Thread(target=_pipe, args=(up, client), daemon=True)
    t.start()
    _pipe(client, up)
    t.join()
    client.close()
    up.close()


def _serve(lsock: socket.socket, path: str):
    while True:
        try:
            c, _ = lsock.accept()
        except OSError:
            return
        threading.Thread(target=_handle, args=(c, path), daemon=True).start()


def parse(argv):
    maps, cmd = [], None
    if "--" in argv:
        i = argv.index("--")
        argv, cmd = argv[:i], argv[i + 1:]
    for a in argv:
        if a in ("-h", "--help"):
            print(__doc__)
            sys.exit(0)
        addr, sep, path = a.partition("=")
        host, _, port = addr.rpartition(":")
        if not sep or not port.isdigit():
            sys.exit(f"forwarder: bad mapping {a!r}; expected HOST:PORT=/path/to.sock")
        maps.append((host or "127.0.0.1", int(port), path))
    if not maps:
        return parse([DEFAULT] + (["--"] + cmd if cmd is not None else []))
    return maps, cmd


def main(argv=None):
    maps, cmd = parse(sys.argv[1:] if argv is None else argv)
    listeners = []
    for host, port, path in maps:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(128)
        listeners.append((s, path))

    child = None
    if cmd:
        child = os.fork()
        if child == 0:
            for s, _ in listeners:
                s.close()
            try:
                os.execvp(cmd[0], cmd)
            except OSError as e:
                sys.stderr.write(f"forwarder: cannot exec {cmd[0]}: {e}\n")
                os._exit(127)

    for s, path in listeners:
        threading.Thread(target=_serve, args=(s, path), daemon=True).start()

    if child is None:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        return 0

    def fwd(sig, _frame):
        try:
            os.kill(child, sig)
        except ProcessLookupError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, fwd)
    # Reap every child (we may be pid 1 in a sandbox) until cmd itself exits.
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
        except InterruptedError:
            continue
        except ChildProcessError:
            return 1
        if pid == child:
            break
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return 128 + os.WTERMSIG(status)


if __name__ == "__main__":
    sys.exit(main())
