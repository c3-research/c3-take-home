"""Per-case proxy tokens.

The grader only needs three things from the proxy (owner: B3):

    mint(case, fixer, arm, budget_usd) -> Token     # before the fixer starts
    usage(token) -> Usage                          # after it exits
    close(token)                                   # revoke / stop accepting requests

`StubProxy` needs no server: tokens are random and usage is zero (for stub
cases and plumbing tests). `HttpProxy` talks to B3's admin API. Usage falls
back to summing `evidence/spend.jsonl` rows for the token (contract ledger
schema), so the grader does not depend on an admin usage endpoint existing.
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from common import ROOT, GraderError


class ProxyInfraError(GraderError):
    """Proxy down or misbehaving: the run is an infrastructure error."""


@dataclass
class Token:
    token: str
    case: str
    fixer: str
    arm: str
    budget_usd: float
    extra: dict = field(default_factory=dict)


@dataclass
class Usage:
    spend_usd: float = 0.0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0


class ProxyClient:
    name = "abstract"
    base_url_host = "http://127.0.0.1:8787"          # as seen by a process-mode fixer
    base_url_docker = "http://127.0.0.1:8787"        # as seen inside the fixer container

    def health(self) -> None: ...
    def upstream_failure(self, tok: "Token") -> str | None: return None
    def mint(self, case: str, fixer: str, arm: str, budget_usd: float) -> Token: raise NotImplementedError
    def usage(self, tok: Token) -> Usage: raise NotImplementedError
    def close(self, tok: Token) -> None: ...

    def fixer_env(self, tok: Token, *, docker: bool) -> dict[str, str]:
        base = self.base_url_docker if docker else self.base_url_host
        return {
            "OPENROUTER_BASE_URL": base + "/v1",
            "OPENAI_BASE_URL": base + "/v1",
            "ANTHROPIC_BASE_URL": base,
            "OPENROUTER_API_KEY": tok.token,
            "OPENAI_API_KEY": tok.token,
            "ANTHROPIC_API_KEY": tok.token,
            "JEV_DECISIONS_URL": base + "/api/alpha/decisions",   # Jev, TypeSafe's decision model (C27)
            "JEV_MODEL": "typesafe/jev-1.13",
        }


def proxy_state_dir() -> Path:
    return Path(os.environ.get("C3_PROXY_STATE", str(ROOT / "proxy" / ".state")))


def proxy_socket() -> Path:
    if os.environ.get("C3_PROXY_SOCK"):
        return Path(os.environ["C3_PROXY_SOCK"])
    for p in (proxy_state_dir() / "proxy.sock", ROOT / "proxy" / "run" / "proxy.sock"):
        if p.exists():
            return p
    return proxy_state_dir() / "proxy.sock"


class StubProxy(ProxyClient):
    name = "stub"

    def mint(self, case, fixer, arm, budget_usd):
        return Token("stub-" + secrets.token_hex(12), case, fixer, arm, budget_usd)

    def usage(self, tok):
        return Usage()


def _ledger_usage(ledger: Path, token: str) -> Usage:
    u = Usage()
    if not ledger.exists():
        return u
    with open(ledger) as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("token") != token:
                continue
            u.requests += 1
            u.spend_usd += float(row.get("cost_usd") or 0.0)
            # clarifications C12: `input` is non-cached input; totals use input_total
            u.input_tokens += int(row.get("input_total") if row.get("input_total") is not None
                                  else (row.get("input") or 0) + (row.get("cached_input") or 0))
            u.cached_input_tokens += int(row.get("cached_input") or 0)
            u.output_tokens += int(row.get("output") or 0)
    u.spend_usd = round(u.spend_usd, 6)
    return u


class HttpProxy(ProxyClient):
    """B3's proxy (proxy/proxy.py). Admin API: POST /admin/tokens, GET /admin/tokens/<ref>,
    POST /admin/tokens/<ref>/revoke, authenticated with X-Admin-Token.

    C3_PROXY_URL          default http://127.0.0.1:8787
    C3_PROXY_DOCKER_URL   base URL reachable from the fixer container
    C3_PROXY_ADMIN_TOKEN  admin secret (default: read $C3_PROXY_STATE/admin.token, state = proxy/.state)
    """
    name = "http"

    def __init__(self, url: str | None = None, ledger: Path | None = None):
        port = os.environ.get("C3_PROXY_PORT", "8787")
        self.url = (url or os.environ.get("C3_PROXY_URL") or os.environ.get("C3_PROXY_ADMIN_URL")
                    or f"http://127.0.0.1:{port}").rstrip("/")
        self.base_url_host = self.url
        # inside the container the sidecar forwarder always listens on 127.0.0.1:8787
        self.base_url_docker = os.environ.get("C3_PROXY_DOCKER_URL", "http://127.0.0.1:8787").rstrip("/")
        self.ledger = Path(ledger or os.environ.get("C3_PROXY_LEDGER") or ROOT / "evidence" / "spend.jsonl")

    def _secret(self) -> str:
        if os.environ.get("C3_PROXY_ADMIN_TOKEN"):
            return os.environ["C3_PROXY_ADMIN_TOKEN"]
        for p in (proxy_state_dir() / "admin.token", ROOT / "proxy" / "run" / "admin.token"):
            if p.exists():
                return p.read_text().strip()
        raise ProxyInfraError(f"no proxy admin secret in {proxy_state_dir()}: is the proxy running?")

    def _call(self, method: str, path: str, body: dict | None = None, timeout=15, admin=True):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("content-type", "application/json")
        if admin:
            req.add_header("X-Admin-Token", self._secret())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise ProxyInfraError(f"proxy {method} {path}: HTTP {e.code} {e.read()[:200]!r}") from e
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ProxyInfraError(f"proxy {method} {path}: {e}") from e

    def health(self):
        r = self._call("GET", "/healthz", timeout=5, admin=False)
        if not r.get("ok"):
            raise ProxyInfraError(f"proxy unhealthy: {r}")
        if r.get("key_loaded") is False and os.environ.get("C3_PROXY_ALLOW_NO_KEY") != "1":
            raise ProxyInfraError("proxy has no upstream key loaded: put OPENROUTER_API_KEY=sk-or-... in .env, then restart the proxy (it reads .env only at start)")

    def mint(self, case, fixer, arm, budget_usd):
        r = self._call("POST", "/admin/tokens",
                       {"budget_usd": budget_usd, "case": case, "fixer": fixer, "arm": arm})
        tok = r.get("token")
        if not tok:
            raise ProxyInfraError(f"proxy mint returned no token: {r}")
        return Token(tok, case, fixer, arm, budget_usd, extra=r)

    def usage(self, tok):
        try:
            r = self._call("GET", f"/admin/tokens/{tok.token}")
            total_in = r.get("total_input_tokens", r.get("input_total"))
            if total_in is None:   # C12: input_tokens counts non-cached input only
                total_in = int(r.get("input_tokens", 0)) + int(r.get("cached_input_tokens", 0))
            return Usage(float(r.get("spent_usd", 0.0)), int(total_in),
                         int(r.get("cached_input_tokens", 0)), int(r.get("output_tokens", 0)),
                         int(r.get("requests", 0)))
        except ProxyInfraError:
            return _ledger_usage(self.ledger, tok.extra.get("id") or tok.token[:12])

    def upstream_failure(self, tok) -> str | None:
        """OpenRouter outage heuristic: most of this token's requests got 5xx."""
        tid = tok.extra.get("id") or tok.token[:12]
        n = bad = 0
        if not self.ledger.exists():
            return None
        with open(self.ledger) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("token") != tid:
                    continue
                n += 1
                try:
                    bad += int(row.get("status") or 0) >= 500
                except (TypeError, ValueError):
                    pass
        if bad >= 3 and bad * 2 > n:
            return f"upstream errors: {bad} of {n} model requests returned 5xx"
        return None

    def close(self, tok):
        try:
            self._call("POST", f"/admin/tokens/{tok.token}/revoke", {})
        except ProxyInfraError:
            pass


def make_proxy(kind: str | None = None) -> ProxyClient:
    """kind: stub | http | auto (default: $C3_PROXY or auto).

    http: the real proxy; if it is down, every case gets infra_error.
    auto: http when the proxy answers at start-up, else a stub (with a warning).
          Use http for real grading runs."""
    kind = kind or os.environ.get("C3_PROXY") or "auto"
    if kind == "stub":
        return StubProxy()
    p = HttpProxy()
    if kind == "http":
        return p
    try:
        p.health()
        return p
    except ProxyInfraError as e:
        import sys
        print(f"grade: WARNING: no model proxy ({e}); using a stub proxy, so model calls will fail. "
              f"Pass --proxy http to make this an infra error.", file=sys.stderr)
        return StubProxy()
