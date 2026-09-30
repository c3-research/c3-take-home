#!/usr/bin/env python3
"""C3 assessment model proxy.

Standard library only. Serves OpenAI-format /v1/chat/completions and
Anthropic-format /v1/messages (both with streaming) on 127.0.0.1:PORT and on a
Unix socket (for sandboxes and --network none containers), and forwards to
OpenRouter with the real key.

    python proxy/server.py [--port 8787] [--upstream https://openrouter.ai/api]
                           [--ledger evidence/spend.jsonl] [--state proxy/.state]

Env var equivalents: C3_PROXY_PORT, C3_PROXY_UPSTREAM, C3_PROXY_LEDGER,
C3_PROXY_STATE, C3_PROXY_SOCK (default <state>/proxy.sock).

Model requests authenticate with a per-case proxy token (x-api-key or
Authorization: Bearer), minted with proxy/mint.py through the admin API. The
admin API needs the secret in <state>/admin.token and is only served on the TCP
listener, never on the Unix socket that sandboxes see.

The upstream key comes from OPENROUTER_API_KEY in this process's environment if
set (mock tests), else from assessment-v2/.env. A key read from .env is only
ever sent to openrouter.ai. It is never logged or written anywhere.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import re
import secrets
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

CANONICAL_MODEL = "anthropic/claude-sonnet-4.6"   # default and only model for fixer tokens
# Jev (TypeSafe's decision model) on the Decisions API route only. Output is free;
# input costs about $0.042 per million tokens. The chat routes stay Sonnet-only.
JEV_MODELS = ("typesafe/jev-1.13", "~typesafe/jev-latest")
JEV_INPUT_USD_PER_TOKEN = 0.042e-6
DECISIONS_PATHS = ("/api/alpha/decisions", "/alpha/decisions")
DESIGNER_MODEL = "anthropic/claude-opus-5.5"      # designer-build tokens only
DESIGNER_ARM = "designer-build"
# Aliases Claude Code, litellm and friends send, per canonical model. Anything else is a 403.
MODEL_ALIASES = {
    CANONICAL_MODEL: re.compile(
        r"^(anthropic/)?claude-sonnet-4[.-]6(-\d{8})?(-latest)?$|^(anthropic/)?sonnet(-4[.-]6)?$", re.I),
    DESIGNER_MODEL: re.compile(
        r"^(anthropic/)?claude-opus-5[.-]5(-\d{8})?(-latest)?$|^(anthropic/)?opus(-5[.-]5)?$", re.I),
}
KNOWN_MODELS = tuple(MODEL_ALIASES)
# litellm-style provider prefixes that are harmless if they resolve to the canonical model.
_PREFIXES = ("openrouter/",)

# USD per million tokens. Sonnet 4.6 per ($3 / $15, cache read 0.1x,
# write 1.25x). Opus 5.5 from OpenRouter's /api/v1/models listing on 29 Sep 2026.
MODEL_PRICING = {
    CANONICAL_MODEL: {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    DESIGNER_MODEL: {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00},
}
PRICING = MODEL_PRICING[CANONICAL_MODEL]

ALLOWED_FIELDS = {
    "messages", "system", "max_tokens", "temperature", "top_p", "stop", "tools",
    "tool_choice", "stream", "cache_control", "thinking", "metadata",
}
# Fields that are rejected outright (web access, provider routing, server tools).
REJECTED_FIELDS = {
    "plugins", "web_search_options", "provider", "models", "route", "mcp_servers",
    "container", "web_search", "online", "tool_resources", "search_parameters",
}
ANTHROPIC_BETA_ALLOW = (
    "prompt-caching-", "interleaved-thinking-", "fine-grained-tool-streaming-",
    "token-efficient-tools-", "claude-code-",
)
MAX_THINKING_BUDGET = 16_000
MAX_OUTPUT_TOKENS = 64_000
DEFAULT_MAX_TOKENS = 8_192
MAX_BODY_BYTES = 64 * 1024 * 1024


class Reject(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def read_env_key(path: Path) -> str | None:
    """Return OPENROUTER_API_KEY from a dotenv file, or None. Never logs the value."""
    try:
        text = path.read_text()
    except OSError:
        return None
    bare = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        k, sep, v = line.partition("=")
        if sep and k.strip() == "OPENROUTER_API_KEY":
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            return v or None
        if not sep and line.startswith("sk-or-") and bare is None:
            bare = line                      # a file holding just the key: accept it
    return bare


def normalize_model(model, allowed=(CANONICAL_MODEL,)) -> str:
    """Map an alias to its canonical model and check it against the token's allowlist."""
    allowed = tuple(allowed)
    if not isinstance(model, str) or not model:
        raise Reject(403, f"model is required; this token may use {', '.join(allowed)}")
    m = model.strip()
    for p in _PREFIXES:
        if m.lower().startswith(p):
            m = m[len(p):]
    canon = None
    if ":" not in m:
        canon = next((c for c, rx in MODEL_ALIASES.items() if rx.match(m)), None)
    if canon is None or canon not in allowed:
        raise Reject(403, f"model {model!r} is not allowed; this token may use {', '.join(allowed)}")
    return canon


def _contains_key(obj, key: str) -> bool:
    if isinstance(obj, dict):
        return key in obj or any(_contains_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_key(v, key) for v in obj)
    return False


def _normalize_cache_control(obj):
    """Force every cache_control to the default 5-minute ephemeral type (budget-safe)."""
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if k == "cache_control":
                obj[k] = {"type": "ephemeral"}
            else:
                _normalize_cache_control(v)
    elif isinstance(obj, list):
        for v in obj:
            _normalize_cache_control(v)


def _check_no_remote_fetch(obj):
    """Reject content that would make the provider fetch a URL (web access)."""
    if isinstance(obj, dict):
        src = obj.get("source")
        if isinstance(src, dict) and src.get("type") == "url":
            raise Reject(400, "remote URL content sources are not allowed")
        iu = obj.get("image_url")
        url = iu.get("url") if isinstance(iu, dict) else iu if isinstance(iu, str) else None
        if isinstance(url, str) and not url.startswith("data:"):
            raise Reject(400, "remote image URLs are not allowed; use data: URLs")
        t = obj.get("type")
        if isinstance(t, str) and (t.startswith("server_tool") or t.endswith("_tool_result") and t != "tool_result"
                                   or t.startswith("mcp_") or t == "container_upload"):
            raise Reject(400, f"content block type {t!r} (server tool) is not allowed")
        for v in obj.values():
            _check_no_remote_fetch(v)
    elif isinstance(obj, list):
        for v in obj:
            _check_no_remote_fetch(v)


def sanitize(body: dict, fmt: str, allowed=(CANONICAL_MODEL,)) -> tuple[dict, dict]:
    """Validate a client request. Returns (upstream_body, info) or raises Reject.

    fmt is "openai" or "anthropic". info has max_tokens, est_input_tokens, dropped,
    cache_injected, stream.
    """
    if not isinstance(body, dict):
        raise Reject(400, "request body must be a JSON object")
    for k in body:
        if k in REJECTED_FIELDS:
            raise Reject(400, f"field {k!r} is not allowed (web, plugins, routing and server tools are disabled)")
    model = normalize_model(body.get("model"), allowed)

    out: dict = {"model": model}
    dropped = []
    for k, v in body.items():
        if k == "model":
            continue
        if k in ALLOWED_FIELDS:
            out[k] = v
        elif k == "max_completion_tokens":
            out.setdefault("max_tokens", v)
        elif k == "stop_sequences" and fmt == "anthropic":
            out[k] = v
        else:
            dropped.append(k)

    msgs = out.get("messages")
    if not isinstance(msgs, list) or not msgs:
        raise Reject(400, "messages must be a non-empty list")

    # max_tokens: required for budgeting; default and clamp.
    mt = out.get("max_tokens", DEFAULT_MAX_TOKENS)
    if not isinstance(mt, int) or isinstance(mt, bool) or mt <= 0:
        raise Reject(400, "max_tokens must be a positive integer")
    mt = min(mt, MAX_OUTPUT_TOKENS)
    out["max_tokens"] = mt

    # Tools: function tools only.
    tools = out.get("tools")
    if tools is not None:
        if not isinstance(tools, list):
            raise Reject(400, "tools must be a list")
        for t in tools:
            if not isinstance(t, dict):
                raise Reject(400, "each tool must be an object")
            if fmt == "openai":
                if t.get("type") != "function" or not isinstance(t.get("function"), dict):
                    raise Reject(400, f"only function tools are allowed (got type {t.get('type')!r})")
            else:
                ttype = t.get("type")
                if ttype not in (None, "custom") or "input_schema" not in t or "name" not in t:
                    raise Reject(400, f"only client (custom) tools are allowed (got type {ttype!r})")
        if not tools:
            out.pop("tools")

    tc = out.get("tool_choice")
    if tc is not None:
        if fmt == "openai":
            ok = tc in ("auto", "none", "required") or (isinstance(tc, dict) and tc.get("type") == "function")
        else:
            ok = isinstance(tc, dict) and tc.get("type") in ("auto", "any", "tool", "none")
        if not ok:
            raise Reject(400, "tool_choice must select function tools only")

    th = out.get("thinking")
    if th is not None:
        if not isinstance(th, dict) or th.get("type") not in ("enabled", "disabled", "adaptive"):
            raise Reject(400, "thinking must be {type: enabled, budget_tokens <= 16000} or disabled")
        if th["type"] == "enabled":
            b = th.get("budget_tokens")
            if not isinstance(b, int) or isinstance(b, bool) or b <= 0 or b > MAX_THINKING_BUDGET:
                raise Reject(400, f"thinking.budget_tokens must be an integer <= {MAX_THINKING_BUDGET}")
            out["thinking"] = {"type": "enabled", "budget_tokens": b}
        elif th["type"] == "adaptive":
            # Adaptive has no budget; pin it to an explicit budget within the cap.
            b = min(MAX_THINKING_BUDGET, mt - 1)
            if b >= 1024:
                out["thinking"] = {"type": "enabled", "budget_tokens": b}
            else:
                out.pop("thinking")
        else:
            out["thinking"] = {"type": "disabled"}

    _check_no_remote_fetch(msgs)
    if "system" in out:
        _check_no_remote_fetch(out["system"])

    # Caching: normalise any client cache_control; inject top-level if none anywhere.
    cache_injected = False
    if _contains_key(out, "cache_control"):
        _normalize_cache_control(out)
    else:
        out["cache_control"] = {"type": "ephemeral"}
        cache_injected = True

    stream = bool(out.get("stream", False))
    out["stream"] = stream
    if fmt == "openai":
        out["usage"] = {"include": True}
        if stream:
            out["stream_options"] = {"include_usage": True}

    est_chars = len(json.dumps([out.get("system"), msgs, out.get("tools")], ensure_ascii=False))
    info = {
        "max_tokens": mt,
        "est_input_tokens": math.ceil(est_chars / 4),
        "dropped": dropped,
        "cache_injected": cache_injected,
        "stream": stream,
    }
    return out, info


def reservation_usd(est_input: int, max_tokens: int, input_mult: float, model: str = CANONICAL_MODEL) -> float:
    p = MODEL_PRICING[model]
    return (est_input * p["input"] * input_mult + max_tokens * p["output"]) / 1e6


def cost_from_usage(u: dict, model: str = CANONICAL_MODEL) -> float:
    """u uses ledger semantics: input = non-cached input (incl. cache writes)."""
    uncached = max(0, u["input"] - u["cache_write"])
    p = MODEL_PRICING[model]
    return (uncached * p["input"] + u["cached_input"] * p["cache_read"]
            + u["cache_write"] * p["cache_write"] + u["output"] * p["output"]) / 1e6


class UsageTracker:
    """Accumulates usage from a response body or stream events, in either format."""

    def __init__(self, fmt: str):
        self.fmt = fmt
        self.seen = False
        self.input = self.cached = self.cache_write = self.output = 0
        self.a_input = 0  # anthropic uncached input
        self.cost: float | None = None
        self.upstream_id: str | None = None

    def feed(self, obj: dict):
        if not isinstance(obj, dict):
            return
        if self.upstream_id is None and isinstance(obj.get("id"), str):
            self.upstream_id = obj["id"]
        if self.fmt == "openai":
            u = obj.get("usage")
            if isinstance(u, dict):
                self.seen = True
                self.input = int(u.get("prompt_tokens") or 0)
                self.output = int(u.get("completion_tokens") or 0)
                d = u.get("prompt_tokens_details") or {}
                self.cached = int(d.get("cached_tokens") or 0)
                self.cache_write = int(d.get("cache_write_tokens") or 0)
                if isinstance(u.get("cost"), (int, float)):
                    self.cost = float(u["cost"])
        else:
            msg = obj.get("message") if obj.get("type") == "message_start" else None
            if isinstance(msg, dict):
                self.upstream_id = msg.get("id", self.upstream_id)
                u = msg.get("usage")
            else:
                u = obj.get("usage")
            if isinstance(u, dict):
                self.seen = True
                if u.get("input_tokens") is not None:
                    self.a_input = int(u["input_tokens"])
                if u.get("cache_read_input_tokens") is not None:
                    self.cached = int(u["cache_read_input_tokens"])
                if u.get("cache_creation_input_tokens") is not None:
                    self.cache_write = int(u["cache_creation_input_tokens"])
                if u.get("output_tokens") is not None:
                    self.output = max(self.output, int(u["output_tokens"]))
                if isinstance(u.get("cost"), (int, float)):
                    self.cost = float(u["cost"])
                self.input = self.a_input + self.cached + self.cache_write

    def usage(self) -> dict:
        """Ledger semantics: input = prompt tokens NOT read from cache (uncached + cache
        writes); cached_input = cache reads; input_total = input + cached_input."""
        return {"input": max(0, self.input - self.cached), "cached_input": self.cached,
                "cache_write": self.cache_write, "output": self.output, "input_total": self.input}


class Token:
    __slots__ = ("token", "id", "budget", "case", "fixer", "arm", "run", "spent", "reserved",
                 "input", "cached_input", "cache_write", "output", "requests", "revoked", "created", "models")

    def __init__(self, token, budget, case=None, fixer=None, arm=None, run=None, **kw):
        self.token = token
        self.id = token[:12]
        self.budget = float(budget)
        self.case, self.fixer, self.arm, self.run = case, fixer, arm, run
        self.spent = float(kw.get("spent", 0.0))
        self.reserved = 0.0
        self.input = int(kw.get("input", 0))
        self.cached_input = int(kw.get("cached_input", 0))
        self.cache_write = int(kw.get("cache_write", 0))
        self.output = int(kw.get("output", 0))
        self.requests = int(kw.get("requests", 0))
        self.revoked = bool(kw.get("revoked", False))
        self.created = kw.get("created") or now_iso()
        self.models = list(kw.get("models") or [CANONICAL_MODEL])

    def public(self) -> dict:
        return {"id": self.id, "case": self.case, "fixer": self.fixer, "arm": self.arm, "run": self.run,
                "budget_usd": self.budget, "spent_usd": round(self.spent, 6),
                "reserved_usd": round(self.reserved, 6),
                "remaining_usd": round(self.budget - self.spent - self.reserved, 6),
                "input_tokens": self.input, "cached_input_tokens": self.cached_input,
                "total_input_tokens": self.input + self.cached_input,
                "cache_write_tokens": self.cache_write, "output_tokens": self.output,
                "requests": self.requests, "revoked": self.revoked, "created": self.created,
                "models": list(self.models)}

    def state(self) -> dict:
        d = self.public()
        d["token"] = self.token
        return d


class Proxy:
    def __init__(self, args):
        self.args = args
        up = args.upstream.rstrip("/")
        if up.endswith("/v1"):
            up = up[:-3]  # we append /v1/... ourselves
        self.upstream = up
        self.key = os.environ.get("OPENROUTER_API_KEY") or None
        self.key_source = "environment" if self.key else None
        if not self.key:
            host = urllib.parse.urlsplit(up).hostname or ""
            if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
                self.key = read_env_key(Path(args.env))
                self.key_source = ".env" if self.key else None
        self.lock = threading.Lock()
        self.ledger_lock = threading.Lock()
        self.tokens: dict[str, Token] = {}
        state_dir = Path(args.state)
        state_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = state_dir / "tokens.json"
        self.ledger_path = Path(args.ledger)
        self.admin_secret = self._load_admin_secret(state_dir / "admin.token")
        self._load_state()

    # --- persistence -----------------------------------------------------------------
    @staticmethod
    def _load_admin_secret(path: Path) -> str:
        if path.exists():
            s = path.read_text().strip()
            if s:
                return s
        path.parent.mkdir(parents=True, exist_ok=True)
        s = "c3a-" + secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(s + "\n")
        return s

    def _load_state(self):
        if not self.state_path or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        for d in data.get("tokens", []):
            tok = d.pop("token")
            budget = d.pop("budget_usd")
            t = Token(tok, budget, d.get("case"), d.get("fixer"), d.get("arm"), d.get("run"),
                      spent=d.get("spent_usd", 0), input=d.get("input_tokens", 0),
                      cached_input=d.get("cached_input_tokens", 0),
                      cache_write=d.get("cache_write_tokens", 0), output=d.get("output_tokens", 0),
                      requests=d.get("requests", 0), revoked=d.get("revoked", False),
                      models=d.get("models"),
                      created=d.get("created"))
            self.tokens[tok] = t

    def _save_state_locked(self):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"tokens": [t.state() for t in self.tokens.values()]}, f)
        os.replace(tmp, self.state_path)

    def ledger(self, rec: dict):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, sort_keys=False) + "\n"
        with self.ledger_lock:
            with open(self.ledger_path, "a") as f:
                f.write(line)

    # --- tokens ------------------------------------------------------------------------
    def minted_total(self) -> float:
        """Budget counted against --mint-cap (all but designer-build tokens). A live token counts
        its full budget; a revoked one only what it spent or still holds in flight (it can spend
        no more), so the cap bounds actual spend while letting finished grading runs free budget."""
        return sum((t.spent + t.reserved) if t.revoked else t.budget
                   for t in self.tokens.values() if DESIGNER_MODEL not in t.models)

    def mint(self, spec: dict, via_unix: bool = False) -> Token:
        budget = spec.get("budget_usd")
        if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget < 0:
            raise Reject(400, "budget_usd must be a non-negative number")
        models = spec.get("models") or [CANONICAL_MODEL]
        if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
            raise Reject(400, "models must be a list of model ids")
        canon = []
        for m in models:
            try:
                c = normalize_model(m, KNOWN_MODELS)
            except Reject:
                raise Reject(400, f"unknown model {m!r}; known: {', '.join(KNOWN_MODELS)}")
            if c not in canon:
                canon.append(c)
        arm = spec.get("arm")
        if DESIGNER_MODEL in canon:
            # Designer-build tokens: Opus 5.5 only, always arm=designer-build.
            if canon != [DESIGNER_MODEL]:
                raise Reject(400, f"{DESIGNER_MODEL} tokens may not allow other models")
            if arm not in (None, "", DESIGNER_ARM):
                raise Reject(400, f"tokens allowing {DESIGNER_MODEL} must use arm={DESIGNER_ARM}")
            arm = DESIGNER_ARM
            if via_unix:
                # Designer practice instances serve admin on the sandbox socket; the
                # designer must not be able to mint itself more build budget.
                raise Reject(403, f"{DESIGNER_MODEL} tokens can only be minted on the TCP admin listener")
        tok = "c3p-" + secrets.token_urlsafe(24)
        t = Token(tok, budget, spec.get("case"), spec.get("fixer"), arm, spec.get("run"), models=canon)
        cap = self.args.mint_cap
        with self.lock:
            if cap is not None and DESIGNER_MODEL not in canon:
                used = self.minted_total()
                if used + budget > cap + 1e-9:
                    raise Reject(403, f"mint cap exceeded: ${used:.2f} of ${cap:.2f} already minted on this "
                                      f"proxy instance; a ${budget:.2f} token does not fit")
            self.tokens[tok] = t
            self._save_state_locked()
        return t

    def find(self, ref: str) -> Token | None:
        with self.lock:
            if ref in self.tokens:
                return self.tokens[ref]
            for t in self.tokens.values():
                if t.id == ref:
                    return t
        return None

    def reserve(self, t: Token, amount: float) -> bool:
        with self.lock:
            if t.revoked:
                raise Reject(401, "token revoked")
            if t.spent + t.reserved + amount > t.budget + 1e-12:
                return False
            t.reserved += amount
            return True

    def settle(self, t: Token, reserved: float, cost: float, usage: dict):
        with self.lock:
            t.reserved = max(0.0, t.reserved - reserved)
            t.spent += cost
            t.input += usage["input"]
            t.cached_input += usage["cached_input"]
            t.cache_write += usage["cache_write"]
            t.output += usage["output"]
            t.requests += 1
            self._save_state_locked()


def _sse_events(resp):
    """Yield (raw_bytes, parsed_json_or_None) per line of an SSE stream."""
    while True:
        line = resp.readline()
        if not line:
            return
        data = None
        s = line.strip()
        if s.startswith(b"data:"):
            payload = s[5:].strip()
            if payload and payload != b"[DONE]":
                try:
                    data = json.loads(payload)
                except ValueError:
                    data = None
        yield line, data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "c3-proxy/1"
    proxy: Proxy = None  # set on the class per server
    via_unix = False

    def address_string(self):
        return "unix" if self.via_unix else str(self.client_address[0])

    def log_message(self, fmt, *args):
        if not self.proxy.args.quiet:
            sys.stderr.write("[proxy] %s %s\n" % (self.address_string(), fmt % args))

    # --- helpers -----------------------------------------------------------------------
    def _send_json(self, status: int, obj, extra_headers=None):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str, fmt: str = "openai"):
        if fmt == "anthropic":
            etype = {400: "invalid_request_error", 401: "authentication_error", 402: "billing_error",
                     403: "permission_error", 404: "not_found_error"}.get(status, "api_error")
            body = {"type": "error", "error": {"type": etype, "message": message}}
        else:
            body = {"error": {"message": message, "code": status, "type": "c3_proxy"}}
        self._send_json(status, body)

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY_BYTES:
            raise Reject(413, "request body too large")
        return self.rfile.read(n) if n else b""

    def _client_token(self) -> str | None:
        k = self.headers.get("x-api-key")
        if k:
            return k.strip()
        a = self.headers.get("Authorization") or ""
        if a.lower().startswith("bearer "):
            return a[7:].strip()
        return None

    def _path(self) -> str:
        p = self.path.split("?", 1)[0]
        if p.startswith("/api/"):
            p = p[4:]
        return p.rstrip("/") or "/"

    # --- routing -----------------------------------------------------------------------
    def do_GET(self):
        p = self._path()
        if p in ("/healthz", "/health"):
            return self._send_json(200, {"ok": True, "model": CANONICAL_MODEL,
                                         "key_loaded": bool(self.proxy.key)})
        if p == "/v1/budget":
            t = self.proxy.find(self._client_token() or "")
            if not t or t.token != self._client_token():
                return self._error(401, "invalid proxy token")
            return self._send_json(200, t.public())
        if p == "/v1/models":
            t = self.proxy.find(self._client_token() or "")
            if not t or t.token != self._client_token():
                return self._error(401, "invalid proxy token")
            return self._send_json(200, {"object": "list", "data": [
                {"id": m, "object": "model", "type": "model", "display_name": m} for m in t.models]})
        if p.startswith("/admin"):
            return self._admin("GET", p)
        self._error(404, "not found")

    def do_POST(self):
        p = self._path()
        try:
            if p.startswith("/admin"):
                self._admin_body = self._read_body()
                return self._admin("POST", p)
            if p == "/v1/chat/completions":
                return self._model("openai", "/v1/chat/completions")
            if p == "/v1/messages":
                return self._model("anthropic", "/v1/messages")
            if p == "/v1/messages/count_tokens":
                return self._count_tokens()
            if p in DECISIONS_PATHS:
                return self._decisions()
            self._read_body()
            self._error(404, "not found")
        except Reject as r:
            self._error(r.status, r.message, "anthropic" if p.startswith("/v1/messages") else "openai")

    # --- admin -------------------------------------------------------------------------
    def _admin(self, method: str, p: str):
        if self.via_unix and not self.proxy.args.admin_on_socket:
            return self._error(403, "admin API is not available on this listener")
        given = self.headers.get("X-Admin-Token") or ""
        a = self.headers.get("Authorization") or ""
        if not given and a.lower().startswith("bearer "):
            given = a[7:].strip()
        if not secrets.compare_digest(given.encode(), self.proxy.admin_secret.encode()):
            return self._error(401, "admin token required")
        parts = [x for x in p.split("/") if x]  # ["admin", "tokens", id?, action?]
        if len(parts) < 2 or parts[1] != "tokens":
            return self._error(404, "not found")
        if method == "POST" and len(parts) == 2:
            try:
                spec = json.loads(getattr(self, "_admin_body", b"") or b"{}")
                t = self.proxy.mint(spec, via_unix=self.via_unix)
            except ValueError:
                return self._error(400, "invalid JSON")
            except Reject as r:
                return self._error(r.status, r.message)
            d = t.public()
            d["token"] = t.token
            return self._send_json(201, d)
        if method == "GET" and len(parts) == 2:
            with self.proxy.lock:
                return self._send_json(200, {"tokens": [t.public() for t in self.proxy.tokens.values()]})
        t = self.proxy.find(parts[2])
        if t is None:
            return self._error(404, "unknown token")
        if method == "GET" and len(parts) == 3:
            return self._send_json(200, t.public())
        if method == "POST" and len(parts) == 4 and parts[3] == "revoke":
            with self.proxy.lock:
                t.revoked = True
                self.proxy._save_state_locked()
            return self._send_json(200, t.public())
        return self._error(404, "not found")

    def _count_tokens(self):
        tok = self._client_token()
        t = self.proxy.find(tok or "")
        if not t or t.token != tok or t.revoked:
            raise Reject(401, "invalid proxy token")
        try:
            body = json.loads(self._read_body() or b"{}")
        except ValueError:
            raise Reject(400, "invalid JSON")
        chars = len(json.dumps([body.get("system"), body.get("messages"), body.get("tools")], ensure_ascii=False))
        return self._send_json(200, {"input_tokens": math.ceil(chars / 4)})

    # --- model requests ----------------------------------------------------------------
    def _model(self, fmt: str, upath: str):
        started = time.time()
        px = self.proxy
        tok = self._client_token()
        t = px.find(tok) if tok else None
        rec = {"ts": now_iso(), "token": t.id if t else None, "case": t.case if t else None,
               "fixer": t.fixer if t else None, "arm": t.arm if t else None, "model": None,
               "input": 0, "cached_input": 0, "cache_write": 0, "output": 0, "cost_usd": 0.0,
               "latency_s": 0.0, "status": None, "fmt": fmt, "stream": False,
               "reserved_usd": 0.0, "cost_source": None, "upstream_id": None, "error": None}

        def finish(status, err=None):
            rec["status"] = status
            rec["error"] = err
            rec["latency_s"] = round(time.time() - started, 3)
            px.ledger(rec)

        try:
            raw = self._read_body()
        except Reject as r:
            finish(r.status, r.message)
            raise
        if t is None or t.token != tok or t.revoked:
            finish(401, "invalid or revoked proxy token")
            raise Reject(401, "invalid or revoked proxy token")
        try:
            body = json.loads(raw)
        except ValueError:
            finish(400, "invalid JSON")
            raise Reject(400, "request body is not valid JSON")
        rec["model"] = body.get("model") if isinstance(body, dict) else None
        try:
            upstream_body, info = sanitize(body, fmt, t.models)
        except Reject as r:
            finish(r.status, r.message)
            raise
        model = upstream_body["model"]
        rec["model"] = model
        rec["stream"] = info["stream"]
        if not px.key:
            finish(503, "proxy has no upstream key")
            raise Reject(503, "proxy has no upstream key configured")

        reserve = reservation_usd(info["est_input_tokens"], info["max_tokens"], px.args.reserve_input_mult, model)
        try:
            ok = px.reserve(t, reserve)
        except Reject as r:
            finish(r.status, r.message)
            raise
        if not ok:
            finish(402, "budget exhausted")
            raise Reject(402, f"budget exhausted: this request may cost up to ${reserve:.4f}, "
                              f"remaining ${t.budget - t.spent - t.reserved:.4f}")
        rec["reserved_usd"] = round(reserve, 6)

        tracker = UsageTracker(fmt)
        state = {"done": False}

        def settle(status, err, completed):
            """Settle budget and write the ledger. Called before the last byte reaches the client."""
            if state["done"]:
                return
            state["done"] = True
            usage = tracker.usage()
            if tracker.cost is not None:
                cost, src = tracker.cost, "upstream"
            elif tracker.seen and (completed or usage["input"]):
                cost, src = cost_from_usage(usage, model), "computed"
            elif status == 200:
                cost, src = reserve, "reservation"  # usage never arrived: assume worst case
            else:
                cost, src = 0.0, "none"
            px.settle(t, reserve, cost, usage)
            rec.update(usage)
            rec["cost_usd"] = round(cost, 8)
            rec["cost_source"] = src
            rec["upstream_id"] = tracker.upstream_id
            finish(status, err)

        try:
            self._forward(fmt, upath, upstream_body, info, tracker, settle)
        finally:
            settle(502, "proxy error before settlement", False)

    def _decisions(self):
        """Jev Decisions API (POST /api/alpha/decisions): Jev models only, charged to the token's budget
        at the upstream-reported cost, logged to the same ledger."""
        started = time.time()
        px = self.proxy
        tok = self._client_token()
        t = px.find(tok) if tok else None
        rec = {"ts": now_iso(), "token": t.id if t else None, "case": t.case if t else None,
               "fixer": t.fixer if t else None, "arm": t.arm if t else None, "model": None,
               "input": 0, "cached_input": 0, "cache_write": 0, "output": 0, "cost_usd": 0.0,
               "latency_s": 0.0, "status": None, "fmt": "decisions", "stream": False,
               "reserved_usd": 0.0, "cost_source": None, "upstream_id": None, "error": None}

        def finish(status, err=None):
            rec["status"], rec["error"] = status, err
            rec["latency_s"] = round(time.time() - started, 3)
            px.ledger(rec)

        raw = self._read_body()
        if t is None or t.token != tok or t.revoked:
            finish(401, "invalid or revoked proxy token")
            raise Reject(401, "invalid or revoked proxy token")
        try:
            body = json.loads(raw)
        except ValueError:
            finish(400, "invalid JSON")
            raise Reject(400, "request body is not valid JSON")
        if not isinstance(body, dict):
            finish(400, "body must be an object")
            raise Reject(400, "request body must be a JSON object")
        model = body.get("model") or JEV_MODELS[0]
        if model not in JEV_MODELS:
            finish(403, f"model {model!r} not allowed on the decisions route")
            raise Reject(403, f"the decisions route serves only {', '.join(JEV_MODELS)}")
        body["model"] = model
        rec["model"] = model
        if not px.key:
            finish(503, "proxy has no upstream key")
            raise Reject(503, "proxy has no upstream key configured")
        est_tokens = math.ceil(len(raw) / 3) + 1000
        reserve = max(0.001, est_tokens * JEV_INPUT_USD_PER_TOKEN * 3)
        if not px.reserve(t, reserve):
            finish(402, "budget exhausted")
            raise Reject(402, f"budget exhausted: remaining ${t.budget - t.spent - t.reserved:.4f}")
        rec["reserved_usd"] = round(reserve, 6)
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {px.key}",
                   "HTTP-Referer": "https://cthree.cloud/assessment", "X-Title": "c3-assessment"}
        req = urllib.request.Request(px.upstream + "/alpha/decisions", data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        status, data, obj = 502, b"", None
        try:
            with urllib.request.urlopen(req, timeout=px.args.upstream_timeout) as resp:
                status, data = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, data = e.code, e.read()
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            px.settle(t, reserve, 0.0, {"input": 0, "cached_input": 0, "cache_write": 0, "output": 0})
            finish(502, f"upstream unavailable: {type(e).__name__}")
            return self._error(502, f"upstream unavailable: {type(e).__name__}")
        try:
            obj = json.loads(data)
        except ValueError:
            obj = None
        u = (obj or {}).get("usage") or {}
        usage = {"input": int(u.get("input_tokens") or 0), "cached_input": 0, "cache_write": 0,
                 "output": int(u.get("output_tokens") or 0)}
        if isinstance(u.get("cost"), (int, float)):
            cost, src = float(u["cost"]), "upstream"
        elif status == 200:
            cost, src = reserve, "reservation"
        else:
            cost, src = 0.0, "none"
        px.settle(t, reserve, cost, usage)
        rec.update(usage)
        rec["cost_usd"], rec["cost_source"] = round(cost, 8), src
        rec["upstream_id"] = (obj or {}).get("id")
        finish(status, None if status == 200 else f"upstream HTTP {status}")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _forward(self, fmt, upath, upstream_body, info, tracker, settle):
        """Send to upstream and relay the response, calling settle(status, err, completed)
        before the final bytes are written to the client."""
        px = self.proxy
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {px.key}",
                   "HTTP-Referer": "https://cthree.cloud/assessment", "X-Title": "c3-assessment"}
        if fmt == "anthropic":
            headers["anthropic-version"] = self.headers.get("anthropic-version") or "2023-06-01"
            betas = [b.strip() for b in (self.headers.get("anthropic-beta") or "").split(",") if b.strip()]
            betas = [b for b in betas if b.startswith(ANTHROPIC_BETA_ALLOW)]
            if betas:
                headers["anthropic-beta"] = ",".join(betas)
        req = urllib.request.Request(px.upstream + upath, data=json.dumps(upstream_body).encode(),
                                     headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=px.args.upstream_timeout)
        except urllib.error.HTTPError as e:
            data = e.read()
            try:
                obj = json.loads(data)
                tracker.feed(obj)
            except ValueError:
                pass
            settle(e.code, f"upstream HTTP {e.code}", False)
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            settle(502, f"upstream unavailable: {type(e).__name__}", False)
            self._error(502, f"upstream unavailable: {type(e).__name__}", fmt)
            return

        with resp:
            ctype = resp.headers.get("Content-Type", "application/json")
            common = {"x-c3-cache-injected": "1" if info["cache_injected"] else "0"}
            if info["dropped"]:
                common["x-c3-dropped-fields"] = ",".join(sorted(info["dropped"]))[:500]
            if not info["stream"] or "text/event-stream" not in ctype:
                data = resp.read()
                try:
                    tracker.feed(json.loads(data))
                except ValueError:
                    pass
                settle(resp.status, None, True)
                self.send_response(resp.status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in common.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_response(resp.status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            for k, v in common.items():
                self.send_header(k, v)
            self.end_headers()
            completed = False
            client_ok = True
            for line, obj in _sse_events(resp):
                if obj is not None:
                    tracker.feed(obj)
                    if fmt == "anthropic" and obj.get("type") == "message_stop":
                        completed = True
                if line.strip() == b"data: [DONE]":
                    completed = True
                if client_ok:
                    try:
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(line), line))
                        self.wfile.flush()
                    except OSError:
                        client_ok = False
                        break  # client went away; stop generating upstream
            if fmt == "openai" and tracker.seen:
                completed = True
            settle(resp.status, None if client_ok else "client disconnected", completed)
            self.close_connection = True
            if client_ok:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass


class TCPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 128

    def server_bind(self):
        socketserver.UnixStreamServer.server_bind(self)
        self.server_name = "unix"
        self.server_port = 0


def build(args):
    px = Proxy(args)
    tcp_h = type("TCPHandler", (Handler,), {"proxy": px, "via_unix": False})
    unix_h = type("UnixHandler", (Handler,), {"proxy": px, "via_unix": True})
    servers = [TCPServer((args.host, args.port), tcp_h)]
    if args.unix:
        up = Path(args.unix)
        up.parent.mkdir(parents=True, exist_ok=True)
        if up.exists() or up.is_socket():
            up.unlink()
        us = UnixServer(str(up), unix_h)
        os.chmod(up, 0o600)
        servers.append(us)
    return px, servers


def parse_args(argv=None):
    E = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=E("C3_PROXY_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(E("C3_PROXY_PORT", "8787")))
    ap.add_argument("--upstream", default=E("C3_PROXY_UPSTREAM", "https://openrouter.ai/api"),
                    help="Upstream base; /v1/chat/completions and /v1/messages are appended")
    ap.add_argument("--ledger", default=E("C3_PROXY_LEDGER", str(ROOT / "evidence" / "spend.jsonl")))
    ap.add_argument("--state", default=E("C3_PROXY_STATE", str(HERE / ".state")),
                    help="state dir: tokens.json, admin.token, proxy.sock")
    ap.add_argument("--unix", default=E("C3_PROXY_SOCK"),
                    help="Unix socket for sandboxes (no admin API); default <state>/proxy.sock; 'none' disables")
    ap.add_argument("--env", default=E("C3_PROXY_ENV_FILE", str(ROOT / ".env")),
                    help="dotenv file holding OPENROUTER_API_KEY (used only for openrouter.ai)")
    ap.add_argument("--reserve-input-mult", type=float, default=1.25,
                    help="input price multiplier for reservations (1.25 = cache-write price, conservative)")
    ap.add_argument("--upstream-timeout", type=float, default=600.0)
    ap.add_argument("--mint-cap", type=float, default=float(E("C3_PROXY_MINT_CAP")) if E("C3_PROXY_MINT_CAP") else None,
                    metavar="USD", help="cap on the total budget of all tokens minted on this instance "
                                        "(designer-build tokens excluded; they have their own budgets)")
    ap.add_argument("--admin-on-socket", action="store_true", default=E("C3_PROXY_ADMIN_ON_SOCKET") == "1",
                    help="also serve the admin API on the Unix socket (dedicated designer practice "
                         "instances only; never the main 8787 instance). Socket mints can't create "
                         "designer-build tokens.")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    if not a.unix:
        a.unix = str(Path(a.state) / "proxy.sock")
    elif a.unix == "none":
        a.unix = ""
    return a


def main(argv=None):
    args = parse_args(argv)
    px, servers = build(args)
    threads = []
    for s in servers[1:]:
        th = threading.Thread(target=s.serve_forever, daemon=True)
        th.start()
        threads.append(th)
    where = f"http://{args.host}:{args.port}" + (f" and unix:{args.unix}" if args.unix else "")
    if args.admin_on_socket or args.mint_cap is not None:
        sys.stderr.write(f"[proxy] designer practice mode: admin on socket={args.admin_on_socket}, "
                         f"mint cap={args.mint_cap}\n")
    sys.stderr.write(f"[proxy] listening on {where}; upstream {px.upstream}; "
                     f"key {'from ' + px.key_source if px.key else 'MISSING'}; ledger {px.ledger_path}\n")
    sys.stderr.flush()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.server_close()
        if args.unix:
            try:
                os.unlink(args.unix)
            except OSError:
                pass


if __name__ == "__main__":
    main()
