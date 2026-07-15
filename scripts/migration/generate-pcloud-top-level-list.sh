#!/usr/bin/env bash
set -Eeuo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
[ -f "$LIB_DIR/config.sh" ] || LIB_DIR="$SCRIPT_DIR/lib"
source "$LIB_DIR/config.sh"
source "$LIB_DIR/safety.sh"
source "$LIB_DIR/checks.sh"
pcm_load_config
pcm_check_rclone_source
pcm_rclone_source lsf "$PCM_SOURCE_SPEC" --max-depth 1 | sed 's#/$##' | sed '/^$/d'
