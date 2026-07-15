#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
[ -f "$LIB_DIR/config.sh" ] || LIB_DIR="$SCRIPT_DIR/lib"
source "$LIB_DIR/config.sh"
source "$LIB_DIR/runtime.sh"
source "$LIB_DIR/checks.sh"
pcm_load_config
pcm_validate_storage_layout

STATE="$PCM_BASE_DIR/proton-upload"
HISTORY="$STATE/status-history.jsonl"
LATEST="$STATE/status-latest.json"
mkdir -p "$STATE" "$PCM_BASE_DIR/locks"
exec 9>"$PCM_BASE_DIR/locks/progress-snapshot.lock"
flock -n 9 || exit 0
temporary="$(mktemp "$STATE/.status-latest.XXXXXX")"
trap 'rm -f "$temporary"' EXIT
"$SCRIPT_DIR/full-sync.sh" status --json | jq -c . > "$temporary"
cat "$temporary" >> "$HISTORY"
cat "$temporary" | pcm_atomic_write "$LATEST"
rm -f "$temporary"
trap - EXIT
