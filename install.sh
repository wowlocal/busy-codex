#!/bin/sh
# Run from any directory; use the macOS runtime verified with foreground detection.
set -eu
cd -- "$(dirname -- "$0")"
if [ "$(uname -s)" = Darwin ]; then
    busy_python=/usr/bin/python3
else
    busy_python=python3
fi
if [ "${1:-}" = --update ]; then
    shift
    if [ -n "$(git status --porcelain)" ]; then
        echo 'Local changes found. Commit or stash them before --update.' >&2
        exit 1
    fi
    git pull --ff-only
    # Re-read the installer after pulling, including updates to this shell script.
    exec ./install.sh "$@"
fi
exec "$busy_python" setup_busy_codex.py "$@"
