#!/usr/bin/env bash
# Candidate-facing grader wrapper (practice cases only).
#
#   ./grade.sh --fixer ./myfixer --cases practice/            # fixer directory: process or local mode
#   ./grade.sh --fixer my-fixer:latest --cases practice/A1    # docker image: docker mode
#
# Fixer directories run in --mode local by default (run.sh as a plain
# subprocess, no sandbox). With C3_SANDBOX=on, and when bwrap works, they run in the bwrap
# sandbox (--mode process). Pass --mode to choose. Real grading uses Docker images.
#
# Any other grade.py option passes through (--concurrency, --runs, --out, --keep ...).
# Results go to results/<timestamp>/ unless --out is given.
#
# The model proxy: if one answers on 127.0.0.1:8787 it is used. Otherwise, if
# proxy/server.py and an OPENROUTER_API_KEY in .env exist, it is started for this
# run. Otherwise grading uses a stub proxy (fixers that call the model will get
# connection errors) and says so.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$HERE/grader/grade.py" ]]; then ROOT="$HERE"; else ROOT="$(dirname "$HERE")"; fi
GRADER="$ROOT/grader"

PY="${C3_GRADER_PYTHON:-}"
if [[ -z "$PY" ]]; then
  for c in "$GRADER/.venv/bin/python" python3.14 python3.13 python3.12 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys, yaml, pytest; assert sys.version_info >= (3, 12)' 2>/dev/null; then
      PY="$(command -v "$c")"; break
    fi
  done
fi
if [[ -z "$PY" ]]; then
  echo "grade.sh: need Python >= 3.12 with PyYAML and pytest (pip install pyyaml pytest)" >&2
  exit 2
fi
export C3_GRADER_PYTHON="$PY"

args=("$@")
have_out=0; have_cases=0; have_proxy=0; have_mode=0; fixer=""; prev=""
for x in "${args[@]}"; do
  case "$x" in --out|--out=*) have_out=1;; --cases|--cases=*) have_cases=1;; --proxy|--proxy=*) have_proxy=1;;
               --mode|--mode=*) have_mode=1;; --fixer=*) fixer="${x#--fixer=}";; esac
  [[ "$prev" == "--fixer" ]] && fixer="$x"
  if [[ "$prev" == "--cases" ]]; then
    case "$x" in
      *hidden*|C[0-9]*|D[0-9]*|E[0-9]*) echo "grade.sh grades practice cases only ($x)" >&2; exit 2;;
    esac
  fi
  prev="$x"
done
if [[ $have_cases -eq 0 ]]; then
  if [[ -d "$ROOT/practice" ]]; then args+=(--cases "$ROOT/practice"); else args+=(--cases "$ROOT/cases/practice"); fi
fi
[[ $have_out -eq 1 ]] || args+=(--out "$ROOT/results/$(date +%Y%m%d-%H%M%S)")
[[ -d "$ROOT/grading" ]] && args+=(--private-root "$ROOT/grading")

bwrap_usable() {
  command -v bwrap >/dev/null 2>&1 && bwrap --ro-bind / / --dev /dev --proc /proc true >/dev/null 2>&1
}
if [[ $have_mode -eq 0 && -n "$fixer" && -d "$fixer" ]]; then
  # Practice default is local mode: the sandbox needs a Python with PyYAML and pytest bound in
  # (/opt/venv), which a candidate machine won't have. Opt in with C3_SANDBOX=on.
  if [[ "${C3_SANDBOX:-off}" != "on" ]] || [[ ! -x "$ROOT/sandbox/run.sh" ]] || ! bwrap_usable; then
    echo "grade.sh: using --mode local (no bwrap sandbox; practice only)" >&2
    args+=(--mode local)
  else
    args+=(--mode process)
  fi
fi

proxy_up() {
  "$PY" - <<'PYEOF' >/dev/null 2>&1
import urllib.request
urllib.request.urlopen("http://127.0.0.1:8787/healthz", timeout=2).read()
PYEOF
}

PROXY_PID=""
cleanup() { [[ -n "$PROXY_PID" ]] && kill "$PROXY_PID" 2>/dev/null || true; }
trap cleanup EXIT

if [[ $have_proxy -eq 0 ]]; then
  if proxy_up; then
    :
  elif [[ -f "$ROOT/proxy/server.py" && -f "$ROOT/.env" ]] && grep -q '^OPENROUTER_API_KEY=' "$ROOT/.env"; then
    mkdir -p "$ROOT/proxy/.state"
    echo "grade.sh: starting the model proxy (log: $ROOT/proxy/.state/server.log)"
    "$PY" "$ROOT/proxy/server.py" --env "$ROOT/.env" >"$ROOT/proxy/.state/server.log" 2>&1 &
    PROXY_PID=$!
    for _ in $(seq 50); do proxy_up && break; sleep 0.2; done
    proxy_up || { echo "grade.sh: the proxy did not start; see $ROOT/proxy/.state/server.log" >&2; exit 2; }
  else
    echo "grade.sh: WARNING: no model proxy and no OPENROUTER_API_KEY in $ROOT/.env;" \
         "grading with a stub proxy (model calls will fail)." >&2
    args+=(--proxy stub)
  fi
fi

rc=0
"$PY" "$GRADER/grade.py" "${args[@]}" || rc=$?
exit $rc      # the EXIT trap stops a proxy we started
