#!/usr/bin/env bash
# Sandboxed fixer run (contracts: grader.md "Sandbox", fixer.md, .
#   sandbox/run.sh --workspace W --corpus C --token T --budget USD --deadline EPOCH \
#                  [--fixer DIR] [--probe SESSION] -- cmd...
# Implementation and full option list: sandbox/_sandbox.sh (run with --help).
C3_SANDBOX_MODE=run exec bash "$(dirname "${BASH_SOURCE[0]}")/_sandbox.sh" "$@"
