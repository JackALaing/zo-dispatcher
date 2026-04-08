#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SECRETS_FILE="${SECRETS_FILE:-$SCRIPT_DIR/.env}"

if [ -f "$SECRETS_FILE" ]; then
    set -a
    source "$SECRETS_FILE"
    set +a
fi

cd "$SCRIPT_DIR"
exec python -u -m zo_dispatcher.server
