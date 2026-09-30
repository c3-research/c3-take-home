#!/usr/bin/env bash
# Shared implementation of sandbox/run.sh and sandbox/designer.sh.
# Contracts: grader.md "Sandbox", fixer.md, clarifications C6, C8, C14.
#
# bwrap --unshare-all (no network namespace plumbing): the only way out is the
# model proxy's Unix socket, bound at /run/c3-proxy.sock and exposed inside as
# 127.0.0.1:8787 by proxy/forwarder.py (which also runs as pid 1 and forwards
# SIGTERM to the command). Designers use the same path for their build model:
# Claude Code -> proxy with a designer-build token (Opus 5.5 only, C14). There is
# no other egress and no Anthropic credential in either sandbox.
set -euo pipefail

MODE="${C3_SANDBOX_MODE:?internal: C3_SANDBOX_MODE not set}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
TA="$(dirname "$ROOT")/technical-assessment"
# shellcheck source=tools.conf
[ -f "$HERE/tools.conf" ] && . "$HERE/tools.conf"
NODE_DIR="${C3_SANDBOX_NODE_DIR:-${NODE_DIR:-}}"
CLAUDE_BIN="${C3_SANDBOX_CLAUDE_BIN:-${CLAUDE_BIN:-}}"

die() { printf '%s: %s\n' "$(basename "$0")" "$*" >&2; exit 2; }
note() { printf '[sandbox] %s\n' "$*" >&2; }

usage() {
  if [ "$MODE" = run ]; then
    cat >&2 <<'EOF'
usage: sandbox/run.sh --workspace DIR --corpus DIR [--token T] [--budget USD] [--deadline EPOCH]
                      [--fixer DIR] [--venv DIR|none] [--home DIR] [--proxy-sock PATH]
                      [--env K=V]... [--tool SRC:/opt/DEST]... [--probe SESSION] [--tty] -- cmd...
EOF
  else
    cat >&2 <<'EOF'
usage: sandbox/designer.sh --workspace DIR --corpus DIR --out DIR --pack DIR --build-token T
                      [--token T] [--budget USD] [--session NAME]
                      [--practice-proxy-sock PATH --practice-admin-secret-file FILE]
                      [--venv DIR|none] [--home DIR] [--proxy-sock PATH] [--env K=V]...
                      [--tool SRC:/opt/DEST]... [--tty] -- cmd...
EOF
  fi
  exit 2
}

WS="" CORPUS="" TOKEN="" BUDGET="" DEADLINE="" FIXER="" VENV="default" HOME_DIR="" OUT="" PACK=""
PROBE="" TTY=0 BUILD_TOKEN="" PRACTICE_SECRET_FILE=""
SOCK="${C3_PROXY_SOCK:-$ROOT/proxy/.state/proxy.sock}"
EXTRA_ENV=() TOOLS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --workspace) WS="$2"; shift 2 ;;
    --corpus) CORPUS="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --budget) BUDGET="$2"; shift 2 ;;
    --deadline) DEADLINE="$2"; shift 2 ;;
    --fixer) FIXER="$2"; shift 2 ;;
    --venv) VENV="$2"; shift 2 ;;
    --home) HOME_DIR="$2"; shift 2 ;;
    --proxy-sock) SOCK="$2"; shift 2 ;;
    --env) EXTRA_ENV+=("$2"); shift 2 ;;
    --tool) TOOLS+=("$2"); shift 2 ;;
    --probe|--session) PROBE="$2"; shift 2 ;;
    --tty) TTY=1; shift ;;
    --out) [ "$MODE" = designer ] || usage; OUT="$2"; shift 2 ;;
    --pack) [ "$MODE" = designer ] || usage; PACK="$2"; shift 2 ;;
    --build-token) [ "$MODE" = designer ] || usage; BUILD_TOKEN="$2"; shift 2 ;;
    --practice-proxy-sock) [ "$MODE" = designer ] || usage; SOCK="$2"; shift 2 ;;
    --practice-admin-secret-file) [ "$MODE" = designer ] || usage; PRACTICE_SECRET_FILE="$2"; shift 2 ;;
    --) shift; break ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1 (use -- before the command)" ;;
  esac
done
[ $# -gt 0 ] || usage
CMD=("$@")
[ -n "$WS" ] && [ -d "$WS" ] || die "--workspace DIR is required and must exist"
[ -n "$CORPUS" ] && [ -d "$CORPUS" ] || die "--corpus DIR is required and must exist"
if [ "$MODE" = designer ]; then
  [ -n "$OUT" ] && [ -n "$PACK" ] || die "--out DIR and --pack DIR are required"
  mkdir -p "$OUT"
  [ -d "$PACK" ] || die "--pack $PACK does not exist"
  [ -n "$BUILD_TOKEN" ] || note "warning: no --build-token; the designer's Claude Code cannot reach its model"
  if [ -n "$PRACTICE_SECRET_FILE" ]; then
    [ -r "$PRACTICE_SECRET_FILE" ] || die "--practice-admin-secret-file $PRACTICE_SECRET_FILE not readable"
    # Designer practice mode: SOCK is the designer's own proxy instance, started with
    # --admin-on-socket --mint-cap USD, so the pack's grade.sh can mint practice tokens.
    PRACTICE_SECRET="$(tr -d '[:space:]' < "$PRACTICE_SECRET_FILE")"
    [ -n "$PRACTICE_SECRET" ] || die "--practice-admin-secret-file is empty"
    [ "$(realpath -m "$SOCK")" != "$(realpath -m "$ROOT/proxy/.state/proxy.sock")" ] || \
      die "practice mode needs a dedicated proxy instance, not the main proxy socket"
  fi
fi
command -v bwrap >/dev/null || die "bwrap not installed"

# --- Refuse to mount anything that is, or contains, a secret area -----------------
FORBID_SRC=("$ROOT/private" "$ROOT/evidence" "$ROOT/contracts" "$ROOT/.env" "$ROOT/proxy/.state"
            "$ROOT/codebases" "$TA" "/home/sam/.ssh" "/home/sam/.claude" "/home/sam/.config"
            "/var/run/docker.sock" "/run/docker.sock")
check_src() {  # $1 = label, $2 = host path
  local p f
  p="$(realpath -m "$2")"
  [ "$p" = / ] && die "$1 may not be /"
  [ "$p" = /home/sam ] || [ "$p" = /home ] && die "$1 may not be $p"
  for f in "${FORBID_SRC[@]}"; do
    f="$(realpath -m "$f")"
    case "$p/" in "$f/"*) die "$1 ($p) is inside forbidden $f" ;; esac
    case "$f/" in "$p/"*) die "$1 ($p) contains forbidden $f" ;; esac
  done
}
check_src --workspace "$WS"; check_src --corpus "$CORPUS"
[ -n "$FIXER" ] && { [ -d "$FIXER" ] || die "--fixer $FIXER missing"; check_src --fixer "$FIXER"; }
[ -n "$OUT" ] && check_src --out "$OUT"
[ -n "$PACK" ] && check_src --pack "$PACK"

# --- Scratch areas ---------------------------------------------------------------
SBX_TMP="$(mktemp -d "${TMPDIR:-/tmp}/c3-sbx.XXXXXX")"
chmod 700 "$SBX_TMP"
OWN_HOME=0
if [ -z "$HOME_DIR" ]; then HOME_DIR="$SBX_TMP/home"; OWN_HOME=1; fi
mkdir -p "$HOME_DIR"
check_src --home "$HOME_DIR"
BWRAP_PID=""
cleanup() {
  local rc=$?
  rm -rf "$SBX_TMP" 2>/dev/null || true
  [ "$OWN_HOME" = 1 ] && rm -rf "$HOME_DIR" 2>/dev/null || true
  exit $rc
}
trap cleanup EXIT

UIDN="$(id -u)"; GIDN="$(id -g)"
mkdir -p "$SBX_TMP/etc"
printf 'root:x:0:0:root:/root:/usr/bin/nologin\ncandidate:x:%s:%s:candidate:/home/candidate:/bin/bash\nnobody:x:65534:65534:nobody:/:/usr/bin/nologin\n' "$UIDN" "$GIDN" > "$SBX_TMP/etc/passwd"
printf 'root:x:0:\ncandidate:x:%s:\nnobody:x:65534:\n' "$GIDN" > "$SBX_TMP/etc/group"
printf '127.0.0.1 localhost c3-sandbox\n::1 localhost\n' > "$SBX_TMP/etc/hosts"

# --- bwrap arguments ---------------------------------------------------------------
A=(--unshare-all --die-with-parent --as-pid-1 --hostname c3-sandbox --clearenv
   --ro-bind /usr /usr --proc /proc --dev /dev --tmpfs /tmp --tmpfs /run --dir /opt)
for d in bin sbin lib lib64; do
  if [ -L "/$d" ]; then A+=(--symlink "$(readlink "/$d")" "/$d")
  elif [ -d "/$d" ]; then A+=(--ro-bind "/$d" "/$d"); fi
done
for f in /etc/ssl /etc/ca-certificates /etc/ld.so.cache /etc/ld.so.conf /etc/ld.so.conf.d \
         /etc/localtime /etc/nsswitch.conf /etc/protocols /etc/services /etc/mime.types; do
  [ -e "$f" ] && A+=(--ro-bind "$f" "$f")
done
A+=(--ro-bind "$SBX_TMP/etc/passwd" /etc/passwd --ro-bind "$SBX_TMP/etc/group" /etc/group
    --ro-bind "$SBX_TMP/etc/hosts" /etc/hosts)
[ "$MODE" = run ] && A+=(--unshare-user --disable-userns)   # designers may nest run.sh, so they keep userns
[ "$TTY" = 1 ] || A+=(--new-session)

A+=(--bind "$WS" /workspace --ro-bind "$CORPUS" /corpus --bind "$HOME_DIR" /home/candidate)
[ -n "$FIXER" ] && A+=(--ro-bind "$FIXER" /fixer)
[ -n "$OUT" ] && A+=(--bind "$OUT" /out)
[ -n "$PACK" ] && A+=(--bind "$PACK" /pack)
A+=(--ro-bind "$ROOT/proxy/forwarder.py" /opt/c3/forwarder.py --ro-bind "$HERE/probe.py" /opt/c3/probe.py)

PATH_IN="/usr/local/bin:/usr/bin"
if [ -n "$NODE_DIR" ] && [ -d "$NODE_DIR" ]; then A+=(--ro-bind "$NODE_DIR" /opt/node); PATH_IN="/opt/node/bin:$PATH_IN"; fi
if [ -n "$CLAUDE_BIN" ] && [ -x "$CLAUDE_BIN" ]; then A+=(--ro-bind "$CLAUDE_BIN" /opt/claude/bin/claude); PATH_IN="/opt/claude/bin:$PATH_IN"; fi
[ "$VENV" = default ] && { if [ -d "$ROOT/.venv" ]; then VENV="$ROOT/.venv"; else VENV=none; fi; }
if [ "$VENV" != none ]; then
  [ -x "$VENV/bin/python" ] || die "--venv $VENV has no bin/python"
  case "$(realpath "$VENV/bin/python")" in /usr/*) ;; *) note "warning: $VENV/bin/python resolves outside /usr; it will not run inside" ;; esac
  A+=(--ro-bind "$VENV" /opt/venv); PATH_IN="/opt/venv/bin:$PATH_IN"
fi
for t in "${TOOLS[@]}"; do
  src="${t%%:*}"; dst="${t#*:}"
  [ -e "$src" ] || die "--tool source $src missing"
  case "$dst" in /opt/*) ;; *) die "--tool destination must be under /opt ($dst)" ;; esac
  check_src --tool "$src"
  A+=(--ro-bind "$src" "$dst")
done

FWD=(python3 /opt/c3/forwarder.py)
HAVE_PROXY=0
if [ -S "$SOCK" ]; then
  A+=(--bind "$SOCK" /run/c3-proxy.sock); HAVE_PROXY=1
else
  note "warning: proxy socket $SOCK not found; 127.0.0.1:8787 inside will not reach a proxy"
fi
FWD+=("127.0.0.1:8787=/run/c3-proxy.sock")

# --- Environment (fixer.md table; designers get proxy info without ANTHROPIC_* overrides)
MODEL="anthropic/claude-sonnet-4.6"
E=(PATH "$PATH_IN" HOME /home/candidate USER candidate LOGNAME candidate LANG C.UTF-8 LC_ALL C.UTF-8
   TMPDIR /tmp TERM "${TERM:-xterm-256color}" SHELL /bin/bash
   C3_WORKSPACE /workspace C3_CORPUS /corpus C3_PROXY_SOCK /run/c3-proxy.sock
   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC 1 DISABLE_AUTOUPDATER 1 DISABLE_TELEMETRY 1
   DISABLE_ERROR_REPORTING 1 PYTHONUNBUFFERED 1)
if [ "$MODE" = run ]; then
  [ -n "$DEADLINE" ] || DEADLINE=$(( $(date +%s) + 900 ))
  E+=(C3_DEADLINE "$DEADLINE" C3_BUDGET_USD "${BUDGET:-0}"
      OPENROUTER_BASE_URL http://127.0.0.1:8787/v1 OPENAI_BASE_URL http://127.0.0.1:8787/v1
      ANTHROPIC_BASE_URL http://127.0.0.1:8787
      OPENROUTER_API_KEY "$TOKEN" OPENAI_API_KEY "$TOKEN" ANTHROPIC_API_KEY "$TOKEN"
      ANTHROPIC_MODEL "$MODEL" ANTHROPIC_DEFAULT_HAIKU_MODEL "$MODEL" ANTHROPIC_DEFAULT_SONNET_MODEL "$MODEL")
  [ -n "$FIXER" ] && E+=(C3_FIXER_DIR /fixer)
  CHDIR=/workspace
else
  # Designer: build model = Claude Code through the proxy with a designer-build token
  # (Opus 5.5 only). The fixer-side proxy token (for practice runs) is C3_PROXY_TOKEN.
  BUILD_MODEL="anthropic/claude-opus-5.5"
  [ -n "$CLAUDE_BIN" ] && [ -x "$CLAUDE_BIN" ] || note "warning: pinned Claude Code ($CLAUDE_BIN) not found"
  E+=(C3_OUT /out C3_PACK /pack C3_PROXY_BASE_URL http://127.0.0.1:8787
      C3_PROXY_OPENAI_BASE_URL http://127.0.0.1:8787/v1 C3_PROXY_TOKEN "$TOKEN" C3_BUDGET_USD "${BUDGET:-0}"
      C3_MODEL "$MODEL"
      ANTHROPIC_BASE_URL http://127.0.0.1:8787 ANTHROPIC_API_KEY "$BUILD_TOKEN"
      ANTHROPIC_MODEL "$BUILD_MODEL" ANTHROPIC_DEFAULT_HAIKU_MODEL "$BUILD_MODEL"
      ANTHROPIC_DEFAULT_SONNET_MODEL "$BUILD_MODEL" ANTHROPIC_DEFAULT_OPUS_MODEL "$BUILD_MODEL"
      ANTHROPIC_SMALL_FAST_MODEL "$BUILD_MODEL" CLAUDE_CODE_SUBAGENT_MODEL "$BUILD_MODEL"
      MAX_THINKING_TOKENS 16000 CLAUDE_CONFIG_DIR /home/candidate/.claude)
  # Practice mode: grade.sh / grader/proxy_client.py read the admin secret from
  # C3_PROXY_ADMIN_TOKEN and reach the admin API at 127.0.0.1:8787 (this instance).
  [ -n "$PRACTICE_SECRET_FILE" ] && E+=(C3_PROXY_ADMIN_TOKEN "$PRACTICE_SECRET" C3_PROXY_URL http://127.0.0.1:8787)
  CHDIR=/workspace
  # Scratch Claude config: no credentials, no history; onboarding done and the proxy
  # token pre-approved so interactive sessions don't stop at the API-key prompt.
  mkdir -p "$HOME_DIR/.claude"; chmod 700 "$HOME_DIR/.claude"
  if [ ! -f "$HOME_DIR/.claude/.claude.json" ]; then
    python3 - "$HOME_DIR/.claude/.claude.json" "$BUILD_TOKEN" <<'PY'
import json, sys
path, tok = sys.argv[1], sys.argv[2]
cfg = {"hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": True,
       "customApiKeyResponses": {"approved": [tok[-20:]] if tok else [], "rejected": []}}
json.dump(cfg, open(path, "w"))
PY
  fi
fi
for kv in "${EXTRA_ENV[@]}"; do
  case "$kv" in *=*) E+=("${kv%%=*}" "${kv#*=}") ;; *) die "--env expects K=V (got $kv)" ;; esac
done
for ((i = 0; i < ${#E[@]}; i += 2)); do A+=(--setenv "${E[i]}" "${E[i+1]}"); done
A+=(--remount-ro / --chdir "$CHDIR")   # root skeleton (/etc, /opt, ...) read-only; binds keep their modes

# --- T5 probe: designer always, run.sh with --probe SESSION --------------------------
if [ "$MODE" = designer ] || [ -n "$PROBE" ]; then
  PARGS=(--session "${PROBE:-adhoc}" --kind "$MODE")
  [ "$HAVE_PROXY" = 1 ] || PARGS+=(--no-require-proxy)
  set +e
  bwrap "${A[@]}" -- "${FWD[@]}" -- python3 /opt/c3/probe.py "${PARGS[@]}" > "$SBX_TMP/probe.json" 2>"$SBX_TMP/probe.err"
  prc=$?
  set -e
  if [ -n "$PROBE" ]; then
    EVDIR="${C3_EVIDENCE_DIR:-$ROOT/evidence/tests}"
    mkdir -p "$EVDIR"
    cp "$SBX_TMP/probe.json" "$EVDIR/T5-$PROBE.json"
    note "T5 probe written to $EVDIR/T5-$PROBE.json"
  fi
  if [ $prc -ne 0 ]; then
    note "T5 probe FAILED (exit $prc); refusing to start the session."
    python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); [print("  FAIL", f["name"], f["target"], f["detail"], file=sys.stderr) for f in d["failures"]]' "$SBX_TMP/probe.json" 2>/dev/null || cat "$SBX_TMP/probe.err" >&2
    exit 90
  fi
  note "T5 probe passed ($(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["n_checks"])' "$SBX_TMP/probe.json") checks)"
fi

# --- Run -----------------------------------------------------------------------------
# SIGTERM/SIGINT to this script are forwarded to the forwarder (pid 1 inside), which
# passes them on to the command; SIGKILL of bwrap kills everything (--die-with-parent).
on_signal() {
  local sig=$1 cpid
  cpid="$(sed -n 's/.*"child-pid": *\([0-9]*\).*/\1/p' "$SBX_TMP/info.json" 2>/dev/null | head -1)"
  if [ -n "$cpid" ]; then kill -s "$sig" "$cpid" 2>/dev/null || true
  elif [ -n "$BWRAP_PID" ]; then kill -s "$sig" "$BWRAP_PID" 2>/dev/null || true; fi
}
trap 'on_signal TERM' TERM
trap 'on_signal INT' INT
trap 'on_signal HUP' HUP
set +e
bwrap --info-fd 3 "${A[@]}" -- "${FWD[@]}" -- "${CMD[@]}" 3>"$SBX_TMP/info.json" &
BWRAP_PID=$!
while :; do
  wait "$BWRAP_PID"; rc=$?
  kill -0 "$BWRAP_PID" 2>/dev/null || break   # wait returned because of a trapped signal
done
set -e
exit $rc
