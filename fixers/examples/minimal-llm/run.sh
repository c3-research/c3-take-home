#!/bin/sh
# Process-mode entry point.
exec python3 "$(dirname "$0")/fix.py" "$@"
