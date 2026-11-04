#!/bin/bash
# Prefer PM's supported bootstrap/selected environment; never execute PM with system Python.
set -u
home="${HERMES_HOME:-/data/hermes}"
source="${AGENT_DIR:-${home}/agent-code}"
[ -d "$source" ] || source=/opt/hermes-agent-code
if [ -x "$source/.hermes/bin/hermes" ]; then
    exec "$source/.hermes/bin/hermes" "$@"
elif [ -x "$source/venv/bin/python" ]; then
    exec "$source/venv/bin/python" "$source/hermes" "$@"
else
    printf '%s\n' 'Hermes runtime unavailable; use /recovery/ and restore PM intent.' >&2
    exit 127
fi
