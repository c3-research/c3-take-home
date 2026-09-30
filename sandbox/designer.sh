#!/usr/bin/env bash
# Designer / dry-run candidate sandbox (contracts: grader.md "Sandbox", clarifications C6/C14).
#   sandbox/designer.sh --workspace W --corpus C --out O --pack P --build-token T [--session NAME] -- cmd...
# /out and /pack are writable. The only network is the proxy: Claude Code (pinned, on
# PATH) uses it via ANTHROPIC_BASE_URL with the designer-build token T (Opus 5.5 only).
# The T5 probe runs first (evidence/tests/T5-<NAME>.json when --session is given).
# Implementation and full option list: sandbox/_sandbox.sh (run with --help).
C3_SANDBOX_MODE=designer exec bash "$(dirname "${BASH_SOURCE[0]}")/_sandbox.sh" "$@"
