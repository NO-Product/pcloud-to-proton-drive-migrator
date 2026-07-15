#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# Status must stay lightweight: this compatibility command never starts a new
# pCloud traversal.
exec "$SCRIPT_DIR/full-sync.sh" inventory-status
