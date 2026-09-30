#!/usr/bin/env python3
"""Mock OpenRouter for testing the proxy offline.

    python proxy/mock_upstream.py --port 9901 [--delay 0.2] [--anthropic-cost]

Serves /api/v1/chat/completions and /api/v1/messages (stream and non-stream).
- Prompt tokens = chars/4 of (system, messages, tools).
- Caching: if the request has cache_control anywhere and the prompt is >= 1024
  tokens, the first request writes the cache and identical later ones read it.
- Output tokens = metadata.mock_output_tokens if given, else min(max_tokens, 20).
- metadata.mock_status = <int> makes it return that HTTP error.
- OpenAI format reports usage.cost (like OpenRouter). Anthropic format omits cost
  unless --anthropic-cost, so the proxy's computed-cost path is exercised.
- GET /_mock/requests returns every request received (body + selected headers);
  POST /_mock/reset clears them and the cache.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

PRICE = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}


def _has(obj, key):
    if isinstance(obj, dict):
        return key in obj or any(_has(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_has(v, key) for v in obj)
    return False


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.requests = []
        self.cache = set()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: State = None
    delay = 0.0
    anthropic_cost = False

    def log_message(self, *a):
        pass

    def _json(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/_mock/requests":
            with self.state.lock:
                return self._json(200, {"requests": self.state.requests})
        self._json(404, {"error": "nf"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        p = self.path.split("?")[0]
        if p == "/_mock/reset":
            with self.state.lock:
                self.state.requests.clear()
                self.state.cache.clear()
            return self._json(200, {"ok": True})
        if p.startswith("/api"):
            p = p[4:]
        body = json.loads(raw or b"{}")
        with self.state.lock:
            self.state.requests.append({"path": p, "body": body, "headers": {
                "authorization": self.headers.get("Authorization"),
                "anthropic-version": self.headers.get("anthropic-version"),
                "anthropic-beta": self.headers.get("anthropic-beta")}})
        md = body.get("metadata") or {}
        if md.get("mock_status"):
            return self._json(int(md["mock_status"]), {"error": {"message": "mock failure"}})
        if self.delay:
            time.sleep(self.delay)
        prompt = math.ceil(len(json.dumps([body.get("system"), body.get("messages"), body.get("tools")],
                                          ensure_ascii=False)) / 4)
        cached = write = 0
        if _has(body, "cache_control") and prompt >= 1024:
            key = hashlib.sha256(json.dumps([body.get("system"), body.get("messages"), body.get("tools")],
                                            sort_keys=True).encode()).hexdigest()
            with self.state.lock:
                if key in self.state.cache:
                    cached = prompt
                else:
                    self.state.cache.add(key)
                    write = prompt
        out = int(md.get("mock_output_tokens") or min(body.get("max_tokens") or 20, 20))
        cost = ((prompt - cached - write) * PRICE["input"] + cached * PRICE["cache_read"]
                + write * PRICE["cache_write"] + out * PRICE["output"]) / 1e6
        text = "mock reply " + " ".join(["tok"] * 5)
        if p == "/v1/chat/completions":
            return self._openai(body, prompt, cached, write, out, cost, text)
        if p == "/v1/messages":
            return self._anthropic(body, prompt, cached, write, out, cost, text)
        self._json(404, {"error": "nf"})

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _openai(self, body, prompt, cached, write, out, cost, text):
        usage = {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out,
                 "prompt_tokens_details": {"cached_tokens": cached, "cache_write_tokens": write},
                 "cost": round(cost, 10)}
        gid = "gen-mock-%d" % time.time_ns()
        if not body.get("stream"):
            return self._json(200, {"id": gid, "object": "chat.completion", "model": body.get("model"),
                                    "choices": [{"index": 0, "finish_reason": "stop",
                                                 "message": {"role": "assistant", "content": text}}],
                                    "usage": usage})
        self._sse_start()
        w = self.wfile.write
        w(b": OPENROUTER PROCESSING\n\n")
        for piece in text.split(" "):
            ch = {"id": gid, "object": "chat.completion.chunk", "model": body.get("model"),
                  "choices": [{"index": 0, "delta": {"content": piece + " "}, "finish_reason": None}]}
            w(b"data: " + json.dumps(ch).encode() + b"\n\n")
        ch = {"id": gid, "object": "chat.completion.chunk", "model": body.get("model"),
              "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        w(b"data: " + json.dumps(ch).encode() + b"\n\n")
        if (body.get("stream_options") or {}).get("include_usage") or (body.get("usage") or {}).get("include"):
            ch = {"id": gid, "object": "chat.completion.chunk", "model": body.get("model"), "choices": [],
                  "usage": usage}
            w(b"data: " + json.dumps(ch).encode() + b"\n\n")
        w(b"data: [DONE]\n\n")
        self.wfile.flush()

    def _anthropic(self, body, prompt, cached, write, out, cost, text):
        usage = {"input_tokens": prompt - cached - write, "cache_creation_input_tokens": write,
                 "cache_read_input_tokens": cached, "output_tokens": out}
        if self.anthropic_cost:
            usage["cost"] = round(cost, 10)
        mid = "msg_mock_%d" % time.time_ns()
        if not body.get("stream"):
            return self._json(200, {"id": mid, "type": "message", "role": "assistant", "model": body.get("model"),
                                    "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                                    "usage": usage})
        self._sse_start()

        def ev(name, obj):
            self.wfile.write(b"event: " + name.encode() + b"\ndata: " + json.dumps(obj).encode() + b"\n\n")

        start_usage = dict(usage, output_tokens=1)
        start_usage.pop("cost", None)
        ev("message_start", {"type": "message_start", "message": {
            "id": mid, "type": "message", "role": "assistant", "model": body.get("model"), "content": [],
            "stop_reason": None, "usage": start_usage}})
        ev("content_block_start", {"type": "content_block_start", "index": 0,
                                   "content_block": {"type": "text", "text": ""}})
        ev("ping", {"type": "ping"})
        for piece in text.split(" "):
            ev("content_block_delta", {"type": "content_block_delta", "index": 0,
                                       "delta": {"type": "text_delta", "text": piece + " "}})
        ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        du = {"output_tokens": out}
        if self.anthropic_cost:
            du["cost"] = usage["cost"]
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": du})
        ev("message_stop", {"type": "message_stop"})
        self.wfile.flush()


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def make(port=0, delay=0.0, anthropic_cost=False):
    h = type("MockH", (H,), {"state": State(), "delay": delay, "anthropic_cost": anthropic_cost})
    return Server(("127.0.0.1", port), h)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9901)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--anthropic-cost", action="store_true")
    a = ap.parse_args()
    srv = make(a.port, a.delay, a.anthropic_cost)
    print(f"mock upstream on http://127.0.0.1:{srv.server_address[1]}/api", flush=True)
    srv.serve_forever()
