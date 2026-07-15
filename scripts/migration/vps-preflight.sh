#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG=/etc/pcloud-proton-migrate/migration.env
QUIET=0
usage() { printf 'usage: %s [--offline] [--quiet] [--config PATH]\n' "$0"; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --offline) shift;;
    --quiet) QUIET=1; shift;;
    --config) CONFIG=${2:?missing path after --config}; shift 2;;
    -h|--help) usage; exit 0;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 64;;
  esac
done
failures=0
ok() { [ "$QUIET" -eq 1 ] || printf 'ok: %s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; failures=$((failures + 1)); }
require_command() { command -v "$1" >/dev/null 2>&1 && ok "tool $1" || fail "missing tool: $1"; }
numeric() { case "$1" in ''|*[!0-9]*) return 1;; *) return 0;; esac; }
positive() { numeric "$1" && [ "$1" -gt 0 ]; }
absolute() { case "$1" in /*) return 0;; *) return 1;; esac; }

[ -r /etc/os-release ] || { fail 'cannot read /etc/os-release'; exit 1; }
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in debian|ubuntu) ok "supported OS ${ID} ${VERSION_ID:-unknown}";; *) fail "unsupported OS: ${ID:-unknown}";; esac
for name in bash python3 sqlite3 jq flock findmnt mountpoint df stat readlink install sha256sum rclone rsync; do require_command "$name"; done
if command -v python3 >/dev/null 2>&1; then
  python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11) or sys.version_info >= (3, 14))' && ok 'Python >= 3.11,<3.14' || fail 'Python >= 3.11,<3.14 is required'
fi

[ -f "$CONFIG" ] || { fail "missing config: $CONFIG"; exit 1; }
config_mode=$(stat -c '%a' "$CONFIG"); config_owner=$(stat -c '%U' "$CONFIG")
[ "$config_owner" = root ] || fail "config must be root-owned: $CONFIG"
[ $((8#$config_mode & 8#022)) -eq 0 ] || fail "config must not be group/world writable: $CONFIG"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../lib/config.sh"
unset PCM_CONFIG_LOADED
MIGRATION_CONFIG=$CONFIG
export MIGRATION_CONFIG
if ! pcm_load_config; then fail 'canonical PCM configuration validation failed'; exit 1; fi
if ! pcm_validate_local_facts; then fail 'local configuration facts failed validation'; fi
required=(
  PCM_BASE_DIR PCM_STAGING_DIR PCM_STORAGE_MOUNT PCM_REQUIRE_MOUNTPOINT
  PCM_SOURCE_REMOTE PCM_SOURCE_ROOT PCM_EXPECTED_PCLOUD_ACCOUNT PCM_ACCOUNT_FINGERPRINT_KEY_FILE PCM_DESTINATION_ROOT
  PCM_EXPECTED_PROTON_ACCOUNT PCM_PROTON_CLI_EXPECTED_VERSION PCM_DESTINATION_CAPACITY_ACKNOWLEDGED PCM_RCLONE_CONFIG
  PCM_PROTON_RUNNER PCM_PROTON_BIN PCM_RUNTIME_USER PCM_MIN_FREE_BYTES PCM_MIN_FREE_INODES
  PCM_INVENTORY_WORKERS PCM_CHECKSUM_WORKERS PCM_DOWNLOAD_TRANSFERS PCM_DOWNLOAD_CHECKERS
  PCM_MULTI_THREAD_STREAMS PCM_LOCAL_VERIFY_WORKERS PCM_UPLOAD_WORKERS
  PCM_DESTINATION_VERIFY_WORKERS PCM_RCLONE_RETRIES PCM_RCLONE_LOW_LEVEL_RETRIES
  PCM_RETRY_SLEEP PCM_MAX_PHASE_ATTEMPTS PCM_PROGRESS_INTERVAL
)
for variable in "${required[@]}"; do [ -n "${!variable:-}" ] || fail "required config variable is empty: $variable"; done

for variable in PCM_BASE_DIR PCM_STAGING_DIR PCM_STORAGE_MOUNT PCM_ACCOUNT_FINGERPRINT_KEY_FILE PCM_RCLONE_CONFIG PCM_PROTON_RUNNER PCM_PROTON_BIN; do
  value=${!variable:-}; [ -z "$value" ] || absolute "$value" || fail "$variable must be an absolute path"
done
case "${PCM_STAGING_DIR:-}" in "${PCM_BASE_DIR:-}"/*) ok 'PCM_STAGING_DIR is within PCM_BASE_DIR';; *) fail 'PCM_STAGING_DIR must be within PCM_BASE_DIR';; esac
case "${PCM_DESTINATION_ROOT:-}" in /*) ok 'PCM_DESTINATION_ROOT is absolute';; *) fail 'PCM_DESTINATION_ROOT must be an absolute destination path';; esac
case "${PCM_SOURCE_REMOTE:-}" in *:) ok 'PCM_SOURCE_REMOTE is an rclone remote';; *) fail 'PCM_SOURCE_REMOTE must end with a colon';; esac
case "${PCM_REQUIRE_MOUNTPOINT:-}" in true|false) ;; *) fail 'PCM_REQUIRE_MOUNTPOINT must be true or false';; esac

if [ -d "${PCM_STORAGE_MOUNT:-}" ]; then
  mount_target=$(findmnt -n -o TARGET --target "$PCM_STORAGE_MOUNT" 2>/dev/null || true)
  mount_source=$(findmnt -n -o SOURCE --target "$PCM_STORAGE_MOUNT" 2>/dev/null || true)
  [ -n "$mount_target" ] && [ -n "$mount_source" ] && ok "storage mount identity $mount_source at $mount_target" || fail 'storage mount is not backed by a mounted filesystem'
  if [ "${PCM_REQUIRE_MOUNTPOINT:-}" = true ]; then
    [ "$mount_target" = "$PCM_STORAGE_MOUNT" ] || fail "PCM_STORAGE_MOUNT is not an exact mountpoint: $PCM_STORAGE_MOUNT"
  fi
  for path in "${PCM_BASE_DIR:-}" "${PCM_STAGING_DIR:-}"; do
    [ -d "$path" ] || { fail "required directory does not exist: $path"; continue; }
    path_target=$(findmnt -n -o TARGET --target "$path" 2>/dev/null || true)
    [ "$path_target" = "$mount_target" ] || fail "$path is not on PCM_STORAGE_MOUNT"
  done
else
  fail "PCM_STORAGE_MOUNT does not exist: ${PCM_STORAGE_MOUNT:-}"
fi

id "${PCM_RUNTIME_USER:-}" >/dev/null 2>&1 || fail "missing PCM_RUNTIME_USER: ${PCM_RUNTIME_USER:-}"
if [ "$(id -u)" -ne 0 ] && [ "$(id -un)" != "${PCM_RUNTIME_USER:-}" ]; then fail 'preflight must run as root or PCM_RUNTIME_USER'; fi
for path in "${PCM_BASE_DIR:-}" "${PCM_STAGING_DIR:-}"; do
  if [ -d "$path" ]; then
    owner=$(stat -c '%U' "$path")
    [ "$owner" = "${PCM_RUNTIME_USER:-}" ] || fail "$path owner is $owner, expected ${PCM_RUNTIME_USER:-}"
    [ -w "$path" ] || fail "$path is not writable by the current runtime user"
  fi
done

if numeric "${PCM_MIN_FREE_BYTES:-}" && [ -d "${PCM_STORAGE_MOUNT:-}" ]; then
  free_bytes=$(( $(df -PB1 --output=avail "$PCM_STORAGE_MOUNT" | tail -n 1) ))
  [ "$free_bytes" -ge "$PCM_MIN_FREE_BYTES" ] && ok "free bytes $free_bytes" || fail "free bytes $free_bytes below required $PCM_MIN_FREE_BYTES"
else fail 'PCM_MIN_FREE_BYTES must be an integer'; fi
if numeric "${PCM_MIN_FREE_INODES:-}" && [ -d "${PCM_STORAGE_MOUNT:-}" ]; then
  free_inodes=$(( $(df -Pi --output=iavail "$PCM_STORAGE_MOUNT" | tail -n 1) ))
  [ "$free_inodes" -ge "$PCM_MIN_FREE_INODES" ] && ok "free inodes $free_inodes" || fail "free inodes $free_inodes below required $PCM_MIN_FREE_INODES"
else fail 'PCM_MIN_FREE_INODES must be an integer'; fi

integer_settings=(PCM_INVENTORY_WORKERS PCM_CHECKSUM_WORKERS PCM_DOWNLOAD_TRANSFERS PCM_DOWNLOAD_CHECKERS PCM_MULTI_THREAD_STREAMS PCM_LOCAL_VERIFY_WORKERS PCM_UPLOAD_WORKERS PCM_DESTINATION_VERIFY_WORKERS PCM_RCLONE_RETRIES PCM_RCLONE_LOW_LEVEL_RETRIES PCM_MAX_PHASE_ATTEMPTS)
for variable in "${integer_settings[@]}"; do positive "${!variable:-}" || fail "$variable must be a positive integer"; done
for variable in PCM_RETRY_SLEEP PCM_PROGRESS_INTERVAL; do
  case "${!variable:-}" in *[!0-9smh]*) fail "$variable must be an integer duration ending in s, m, or h";; [0-9]*[smh]) ;; *) fail "$variable must be an integer duration ending in s, m, or h";; esac
done

for variable in PCM_RCLONE_CONFIG PCM_ACCOUNT_FINGERPRINT_KEY_FILE PCM_PROTON_RUNNER PCM_PROTON_BIN; do
  path=${!variable:-}
  if [ ! -f "$path" ]; then fail "$variable is not a regular file: $path"; continue; fi
  mode=$(stat -c '%a' "$path"); owner=$(stat -c '%U' "$path")
  [ $((8#$mode & 8#022)) -eq 0 ] || fail "$variable is group/world writable: $path"
  case "$variable" in
    PCM_RCLONE_CONFIG)
      [ $((8#$mode & 8#077)) -eq 0 ] || fail 'PCM_RCLONE_CONFIG must have mode 0600 or stricter'
      case "$owner" in root|"${PCM_RUNTIME_USER:-}") ;; *) fail "PCM_RCLONE_CONFIG has unexpected owner $owner";; esac;;
    PCM_ACCOUNT_FINGERPRINT_KEY_FILE)
      [ "$mode" = 600 ] || fail 'PCM_ACCOUNT_FINGERPRINT_KEY_FILE must have mode 0600'
      [ "$owner" = "${PCM_RUNTIME_USER:-}" ] || fail "PCM_ACCOUNT_FINGERPRINT_KEY_FILE owner is $owner, expected ${PCM_RUNTIME_USER:-}"
      key_bytes=$(stat -c '%s' "$path")
      [ "$key_bytes" -ge 32 ] || fail 'PCM_ACCOUNT_FINGERPRINT_KEY_FILE must contain at least 32 bytes';;
    *) [ -x "$path" ] || fail "$variable is not executable: $path"; [ "$owner" = root ] || fail "$variable owner is $owner, expected root";;
  esac
  [ -r "$path" ] || fail "$variable is not readable by the current runtime user"
done

[ "$failures" -eq 0 ] || { printf 'preflight failed with %d error(s)\n' "$failures" >&2; exit 1; }
ok 'offline preflight passed'
