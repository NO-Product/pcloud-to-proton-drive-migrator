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
pcm_validate_storage_layout
pcm_check_capacity >/dev/null

OPERATION_DIR="${PCM_OPERATION_DIR:?PCM_OPERATION_DIR must identify the managed download operation}"

reject_staging_symlinks() {
  local path="$PCM_STAGING_DIR" component current=/ link
  case "$path" in /*) ;; *) printf 'staging path must be absolute\n' >&2; return 2 ;; esac
  IFS=/ read -r -a components <<<"${path#/}"
  for component in "${components[@]}"; do
    [ -n "$component" ] || continue
    current="${current%/}/$component"
    [ ! -L "$current" ] || { printf 'staging path contains a symlink component: %s\n' "$current" >&2; return 2; }
  done
  if [ -d "$path" ]; then
    link="$(find -P "$path" -type l -print -quit)"
    [ -z "$link" ] || { printf 'staging contains a symlink: %s\n' "$link" >&2; return 2; }
  elif [ -e "$path" ]; then
    printf 'staging root is not a directory: %s\n' "$path" >&2
    return 2
  fi
}

reject_staging_symlinks
mkdir -p "$PCM_STAGING_DIR" "$OPERATION_DIR"

[ -f "$OPERATION_DIR/binding.json" ] || { printf 'download snapshot binding is missing\n' >&2; exit 2; }
pcm_rclone_source copy "$PCM_SOURCE_SPEC" "$PCM_STAGING_DIR" \
  --create-empty-src-dirs \
  --transfers "$PCM_DOWNLOAD_TRANSFERS" \
  --checkers "$PCM_DOWNLOAD_CHECKERS" \
  --multi-thread-streams "$PCM_MULTI_THREAD_STREAMS" \
  --retries "$PCM_RCLONE_RETRIES" \
  --low-level-retries "$PCM_RCLONE_LOW_LEVEL_RETRIES" \
  --retries-sleep "$PCM_RETRY_SLEEP" \
  --stats "$PCM_PROGRESS_INTERVAL" \
  --stats-one-line-date \
  --stats-log-level NOTICE
pcm_check_capacity > "$OPERATION_DIR/capacity-after.json"
