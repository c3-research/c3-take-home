#!/bin/sh
# Process-mode entry point (clarifications C10).
exec python3 "$(dirname "$0")/fix.py" "$@"
