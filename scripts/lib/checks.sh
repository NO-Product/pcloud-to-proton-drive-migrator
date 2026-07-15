#!/usr/bin/env bash

pcm_path_below() {
  local child="${1%/}" parent="${2%/}"
  [ "$child" = "$parent" ] || [[ "$child" == "$parent/"* ]]
}

pcm_reject_symlink_components() {
  local path="$1" current=/ component
  [[ "$path" == /* ]] || { printf 'path safety error: absolute path required\n' >&2; return 2; }
  IFS=/ read -r -a components <<<"${path#/}"
  for component in "${components[@]}"; do
    [ -n "$component" ] || continue
    current="${current%/}/$component"
    [ ! -L "$current" ] || { printf 'path safety error: symlink component is forbidden: %s\n' "$current" >&2; return 2; }
  done
}

pcm_validate_storage_layout() {
  local mount_real base_real staging_real mount_target
  [ -d "$PCM_STORAGE_MOUNT" ] || { printf 'capacity error: storage mount does not exist: %s\n' "$PCM_STORAGE_MOUNT" >&2; return 2; }
  mount_target="$(findmnt -n -o TARGET --target "$PCM_STORAGE_MOUNT" 2>/dev/null || true)"
  [ -n "$mount_target" ] || { printf 'capacity error: storage path is not backed by a mounted filesystem\n' >&2; return 2; }
  if [ "$PCM_REQUIRE_MOUNTPOINT" = true ]; then
    [ "$mount_target" = "${PCM_STORAGE_MOUNT%/}" ] || { printf 'capacity error: required mount is not mounted: %s\n' "$PCM_STORAGE_MOUNT" >&2; return 2; }
  fi
  pcm_reject_symlink_components "$PCM_STAGING_DIR"
  mount_real="$(readlink -f -- "$PCM_STORAGE_MOUNT")" || return 2
  base_real="$(readlink -m -- "$PCM_BASE_DIR")" || return 2
  staging_real="$(readlink -m -- "$PCM_STAGING_DIR")" || return 2
  pcm_path_below "$base_real" "$mount_real" || { printf 'path safety error: canonical PCM_BASE_DIR escapes PCM_STORAGE_MOUNT\n' >&2; return 2; }
  pcm_path_below "$staging_real" "$base_real" || { printf 'path safety error: canonical PCM_STAGING_DIR escapes PCM_BASE_DIR\n' >&2; return 2; }
  pcm_path_below "$staging_real" "$mount_real" || { printf 'path safety error: canonical PCM_STAGING_DIR escapes PCM_STORAGE_MOUNT\n' >&2; return 2; }
}

pcm_provision_storage_paths() {
  pcm_validate_storage_layout
  install -d -o "$PCM_RUNTIME_USER" -g "$(id -gn "$PCM_RUNTIME_USER")" -m 0700 "$PCM_BASE_DIR" "$PCM_STAGING_DIR"
  pcm_validate_storage_layout
}

pcm_ensure_account_fingerprint_key() {
  local key="$PCM_ACCOUNT_FINGERPRINT_KEY_FILE" directory temporary group
  group="$(id -gn "$PCM_RUNTIME_USER")"
  directory="$(dirname "$key")"
  pcm_reject_symlink_components "$directory"
  install -d -o root -g "$group" -m 0750 "$directory"
  [ ! -L "$key" ] || { printf 'config error: fingerprint key must not be a symlink\n' >&2; return 2; }
  if [ ! -e "$key" ]; then
    temporary="$(mktemp "$directory/.account-fingerprint-key.XXXXXX")"
    chmod 0600 "$temporary"
    dd if=/dev/urandom of="$temporary" bs=32 count=1 status=none
    chown "$PCM_RUNTIME_USER:$group" "$temporary"
    mv -f -- "$temporary" "$key"
  fi
  [ -f "$key" ] && [ "$(stat -c '%a' "$key")" = 600 ] || { printf 'config error: fingerprint key must be a regular mode-0600 file\n' >&2; return 2; }
  [ "$(stat -c '%U' "$key")" = "$PCM_RUNTIME_USER" ] || { printf 'config error: fingerprint key must be owned by PCM_RUNTIME_USER\n' >&2; return 2; }
  [ "$(stat -c '%s' "$key")" -ge 32 ] || { printf 'config error: fingerprint key must contain at least 32 bytes\n' >&2; return 2; }
}

pcm_check_capacity() {
  local free_bytes free_inodes
  pcm_validate_storage_layout
  pcm_path_below "$PCM_BASE_DIR" "$PCM_STORAGE_MOUNT" || { printf 'capacity error: PCM_BASE_DIR is outside PCM_STORAGE_MOUNT\n' >&2; return 2; }
  pcm_path_below "$PCM_STAGING_DIR" "$PCM_STORAGE_MOUNT" || { printf 'capacity error: PCM_STAGING_DIR is outside PCM_STORAGE_MOUNT\n' >&2; return 2; }
  free_bytes="$(df -PB1 --output=avail "$PCM_STORAGE_MOUNT" | awk 'NR==2 {print $1}')"
  free_inodes="$(df -Pi --output=iavail "$PCM_STORAGE_MOUNT" | awk 'NR==2 {print $1}')"
  [[ "$free_bytes" =~ ^[0-9]+$ && "$free_inodes" =~ ^[0-9]+$ ]] || { printf 'capacity error: could not read free bytes/inodes\n' >&2; return 2; }
  [ "$free_bytes" -ge "$PCM_MIN_FREE_BYTES" ] || { printf 'capacity error: free bytes %s are below required reserve %s\n' "$free_bytes" "$PCM_MIN_FREE_BYTES" >&2; return 2; }
  [ "$free_inodes" -ge "$PCM_MIN_FREE_INODES" ] || { printf 'capacity error: free inodes %s are below required reserve %s\n' "$free_inodes" "$PCM_MIN_FREE_INODES" >&2; return 2; }
  printf '{"free_bytes":%s,"required_free_bytes":%s,"free_inodes":%s,"required_free_inodes":%s}\n' "$free_bytes" "$PCM_MIN_FREE_BYTES" "$free_inodes" "$PCM_MIN_FREE_INODES"
}

pcm_check_rclone_source() {
  local remote="${PCM_SOURCE_REMOTE%:}" type redacted
  [ -f "$PCM_RCLONE_CONFIG" ] || { printf 'doctor error: rclone config does not exist: %s\n' "$PCM_RCLONE_CONFIG" >&2; return 2; }
  redacted="$(rclone --config "$PCM_RCLONE_CONFIG" config redacted 2>/dev/null)" || {
    printf 'config error: rclone could not produce redacted configuration output\n' >&2
    return 2
  }
  type="$(printf '%s\n' "$redacted" | awk -v section="[$remote]" '
    $0 == section {inside=1; next}
    /^\[/ {inside=0}
    inside && $1 == "type" && $2 == "=" {print $3; exit}
  ')"
  [ "$type" = pcloud ] || { printf 'config error: PCM_SOURCE_REMOTE must name a pcloud remote\n' >&2; return 2; }
}

pcm_validate_local_facts() {
  local failed=0
  command -v rclone >/dev/null 2>&1 || { printf 'config error: rclone is required for redacted remote validation\n' >&2; failed=1; }
  [ -f "$PCM_RCLONE_CONFIG" ] && [ -r "$PCM_RCLONE_CONFIG" ] || { printf 'config error: PCM_RCLONE_CONFIG must be a readable regular file\n' >&2; failed=1; }
  [ -f "$PCM_PROTON_RUNNER" ] && [ -x "$PCM_PROTON_RUNNER" ] || { printf 'config error: PCM_PROTON_RUNNER must be an executable regular file\n' >&2; failed=1; }
  [ -f "$PCM_PROTON_BIN" ] && [ -x "$PCM_PROTON_BIN" ] || { printf 'config error: PCM_PROTON_BIN must be an executable regular file\n' >&2; failed=1; }
  [ -f "$PCM_ACCOUNT_FINGERPRINT_KEY_FILE" ] && [ -r "$PCM_ACCOUNT_FINGERPRINT_KEY_FILE" ] || { printf 'config error: PCM_ACCOUNT_FINGERPRINT_KEY_FILE must be a runtime-readable regular file\n' >&2; failed=1; }
  [ ! -e "$PCM_ACCOUNT_FINGERPRINT_KEY_FILE" ] || [ "$(stat -c '%a' "$PCM_ACCOUNT_FINGERPRINT_KEY_FILE")" = 600 ] || { printf 'config error: PCM_ACCOUNT_FINGERPRINT_KEY_FILE must have mode 0600\n' >&2; failed=1; }
  id "$PCM_RUNTIME_USER" >/dev/null 2>&1 || { printf 'config error: PCM_RUNTIME_USER does not exist\n' >&2; failed=1; }
  [ -d "$PCM_STORAGE_MOUNT" ] || { printf 'config error: PCM_STORAGE_MOUNT must be an existing directory\n' >&2; failed=1; }
  [ -d "$PCM_BASE_DIR" ] || { printf 'config error: PCM_BASE_DIR must be an existing directory\n' >&2; failed=1; }
  [ -d "$PCM_STAGING_DIR" ] || { printf 'config error: PCM_STAGING_DIR must be an existing directory\n' >&2; failed=1; }
  pcm_validate_storage_layout || failed=1
  if [ "$PCM_REQUIRE_MOUNTPOINT" = true ] && ! mountpoint -q "$PCM_STORAGE_MOUNT"; then
    printf 'config error: PCM_STORAGE_MOUNT is not the required mountpoint\n' >&2
    failed=1
  fi
  [ "$failed" -eq 0 ] || return 2
  pcm_check_rclone_source
}

pcm_doctor() {
  local command failed=0
  for command in bash jq flock rclone sqlite3 python3 findmnt readlink mountpoint df awk sha256sum; do
    if ! command -v "$command" >/dev/null 2>&1; then
      printf 'missing command: %s\n' "$command" >&2
      failed=1
    fi
  done
  python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 11) or sys.version_info >= (3, 14))' || {
    printf 'unsupported Python version: require >=3.11,<3.14\n' >&2
    failed=1
  }
  id "$PCM_RUNTIME_USER" >/dev/null 2>&1 || { printf 'missing runtime user: %s\n' "$PCM_RUNTIME_USER" >&2; failed=1; }
  [ -x "$PCM_PROTON_RUNNER" ] || { printf 'runner is not executable: %s\n' "$PCM_PROTON_RUNNER" >&2; failed=1; }
  [ -x "$PCM_PROTON_BIN" ] || { printf 'Proton binary is not executable: %s\n' "$PCM_PROTON_BIN" >&2; failed=1; }
  pcm_validate_local_facts || failed=1
  pcm_check_capacity >/dev/null || failed=1
  [ "$failed" -eq 0 ] || return 2
  printf '{"status":"ok","config":"%s","source_remote":"%s","storage_mount":"%s"}\n' "$PCM_CONFIG_FILE" "$PCM_SOURCE_REMOTE" "$PCM_STORAGE_MOUNT"
}
