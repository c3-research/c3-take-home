#!/usr/bin/env python3
"""Minimal LLM fixer: the simplest thing that uses the model. A starting point, not a strategy.

One call to Sonnet 4.6 through the proxy: send the ticket, the log tails and the whole of src/, ask for
the changed files back in full, and write them. It never replays the simulator, reads the corpus or checks
its own patch, so expect it to fix little. Standard library only.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WS = Path(os.environ["C3_WORKSPACE"])
SRC = WS / "src"
LOG_TAIL_LINES = 60

SYSTEM = """You fix bugs in a small distributed Python service.
Reply with every file you change, in full, in exactly this format and nothing else:

=== FILE: src/<path> ===
<the complete new file content>
=== END ===

Only change files under src/. Keep all other code as it is."""


def read(p: Path, limit: int | None = None) -> str:
    text = p.read_text(errors="replace")
    if limit is not None:
        text = "\n".join(text.splitlines()[-limit:])
    return text


def build_prompt() -> str:
    parts = ["# Ticket\n", read(WS / "SYMPTOM.md"), "\n# Log tails\n"]
    for log in sorted((WS / "logs").glob("*.log")):
        parts.append(f"\n## {log.name}\n{read(log, LOG_TAIL_LINES)}\n")
    parts.append("\n# Source\n")
    for f in sorted(SRC.rglob("*.py")):
        if "__pycache__" not in f.parts:
            parts.append(f"\n=== {f.relative_to(WS)} ===\n{read(f)}\n")
    parts.append("\nFind the bug(s) behind the ticket and fix them.")
    return "".join(parts)


def call_model(prompt: str) -> str:
    """One chat completion through the proxy (OpenAI format). Returns the reply text, or '' on error."""
    body = {
        "model": os.environ["ANTHROPIC_MODEL"],          # anthropic/claude-sonnet-4.6
        "max_tokens": 8000,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        os.environ["OPENAI_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
                 "Content-Type": "application/json"})
    timeout = max(30, int(os.environ["C3_DEADLINE"]) - int(time.time()) - 30)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            reply = json.loads(r.read())
    except urllib.error.HTTPError as e:            # 402 over budget, 403 wrong model, 400 bad request
        print(f"model call failed: HTTP {e.code} {e.read()[:300]!r}")
        return ""
    except Exception as e:  # noqa: BLE001
        print(f"model call failed: {e}")
        return ""
    return reply["choices"][0]["message"]["content"] or ""


def write_files(reply: str) -> int:
    """Write each returned file that already exists under src/. Returns how many were written."""
    written = 0
    for path, content in re.findall(r"=== FILE: (\S+) ===\n(.*?)\n=== END ===", reply, re.S):
        target = (WS / path).resolve()
        if SRC.resolve() not in target.parents or not target.exists():
            print(f"skipping {path}: not an existing file under src/")
            continue
        target.write_text(content.rstrip("\n") + "\n")
        print(f"wrote {path}")
        written += 1
    return written


def main() -> int:
    prompt = build_prompt()
    print(f"prompt: {len(prompt)} chars; budget ${os.environ.get('C3_BUDGET_USD')}")
    reply = call_model(prompt)
    n = write_files(reply)
    print(f"done: {n} file(s) changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
