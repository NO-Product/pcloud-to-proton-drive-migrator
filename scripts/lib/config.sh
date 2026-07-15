#!/usr/bin/env bash

pcm_config_error() {
  printf 'config error: %s\n' "$*" >&2
  return 2
}

pcm_config_keys() {
  cat <<'EOF'
PCM_BASE_DIR
PCM_STAGING_DIR
PCM_STORAGE_MOUNT
PCM_REQUIRE_MOUNTPOINT
PCM_SOURCE_REMOTE
PCM_SOURCE_ROOT
PCM_EXPECTED_PCLOUD_ACCOUNT
PCM_ACCOUNT_FINGERPRINT_KEY_FILE
PCM_DESTINATION_ROOT
PCM_EXPECTED_PROTON_ACCOUNT
PCM_PROTON_CLI_EXPECTED_VERSION
PCM_DESTINATION_CAPACITY_ACKNOWLEDGED
PCM_RCLONE_CONFIG
PCM_PROTON_RUNNER
PCM_PROTON_BIN
PCM_RUNTIME_USER
PCM_MIN_FREE_BYTES
PCM_MIN_FREE_INODES
PCM_INVENTORY_WORKERS
PCM_CHECKSUM_WORKERS
PCM_DOWNLOAD_TRANSFERS
PCM_DOWNLOAD_CHECKERS
PCM_MULTI_THREAD_STREAMS
PCM_LOCAL_VERIFY_WORKERS
PCM_UPLOAD_WORKERS
PCM_DESTINATION_VERIFY_WORKERS
PCM_RCLONE_RETRIES
PCM_RCLONE_LOW_LEVEL_RETRIES
PCM_RETRY_SLEEP
PCM_MAX_PHASE_ATTEMPTS
PCM_PROGRESS_INTERVAL
EOF
}

pcm_known_config_key() {
  case "$1" in
    PCM_BASE_DIR|PCM_STAGING_DIR|PCM_STORAGE_MOUNT|PCM_REQUIRE_MOUNTPOINT|PCM_SOURCE_REMOTE|PCM_SOURCE_ROOT|PCM_EXPECTED_PCLOUD_ACCOUNT|PCM_ACCOUNT_FINGERPRINT_KEY_FILE|PCM_DESTINATION_ROOT|PCM_EXPECTED_PROTON_ACCOUNT|PCM_PROTON_CLI_EXPECTED_VERSION|PCM_DESTINATION_CAPACITY_ACKNOWLEDGED|PCM_RCLONE_CONFIG|PCM_PROTON_RUNNER|PCM_PROTON_BIN|PCM_RUNTIME_USER|PCM_MIN_FREE_BYTES|PCM_MIN_FREE_INODES|PCM_INVENTORY_WORKERS|PCM_CHECKSUM_WORKERS|PCM_DOWNLOAD_TRANSFERS|PCM_DOWNLOAD_CHECKERS|PCM_MULTI_THREAD_STREAMS|PCM_LOCAL_VERIFY_WORKERS|PCM_UPLOAD_WORKERS|PCM_DESTINATION_VERIFY_WORKERS|PCM_RCLONE_RETRIES|PCM_RCLONE_LOW_LEVEL_RETRIES|PCM_RETRY_SLEEP|PCM_MAX_PHASE_ATTEMPTS|PCM_PROGRESS_INTERVAL) return 0 ;;
    *) return 1 ;;
  esac
}

pcm_trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

pcm_load_config() {
  [ "${PCM_CONFIG_LOADED:-0}" = 1 ] && return 0
  local file="${MIGRATION_CONFIG:-}" raw line key value quote line_number=0
  local -A seen=()

  [ -n "$file" ] || pcm_config_error 'use --config PATH or set MIGRATION_CONFIG'
  [ -f "$file" ] || pcm_config_error "file does not exist: $file"
  [ -r "$file" ] || pcm_config_error "file is not readable: $file"

  while IFS= read -r raw || [ -n "$raw" ]; do
    line_number=$((line_number + 1))
    line="${raw%$'\r'}"
    line="$(pcm_trim "$line")"
    [ -n "$line" ] || continue
    [[ "$line" == \#* ]] && continue
    [[ "$line" == *=* ]] || pcm_config_error "$file:$line_number: expected PCM_NAME=value"
    key="$(pcm_trim "${line%%=*}")"
    value="$(pcm_trim "${line#*=}")"
    [[ "$key" =~ ^PCM_[A-Z0-9_]+$ ]] || pcm_config_error "$file:$line_number: only PCM_* keys are accepted"
    pcm_known_config_key "$key" || pcm_config_error "$file:$line_number: unknown key $key"
    [ -z "${seen[$key]:-}" ] || pcm_config_error "$file:$line_number: duplicate key $key"
    seen[$key]=1
    if [ "${#value}" -ge 2 ]; then
      quote="${value:0:1}"
      if { [ "$quote" = "'" ] || [ "$quote" = '"' ]; } && [ "${value: -1}" = "$quote" ]; then
        value="${value:1:${#value}-2}"
      fi
    fi
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || pcm_config_error "$file:$line_number: multiline values are forbidden"
    printf -v "$key" '%s' "$value"
    case "$key" in
      PCM_EXPECTED_PCLOUD_ACCOUNT|PCM_EXPECTED_PROTON_ACCOUNT) export -n "$key" ;;
      *) export "$key" ;;
    esac
  done < "$file"

  while IFS= read -r key; do
    [ -n "${seen[$key]:-}" ] || pcm_config_error "missing required key $key"
    [ -n "${!key}" ] || pcm_config_error "empty required key $key"
  done < <(pcm_config_keys)

  PCM_CONFIG_FILE="$file"
  PCM_CONFIG_LOADED=1
  export PCM_CONFIG_FILE PCM_CONFIG_LOADED
  pcm_validate_config
}

pcm_integer_between() {
  local key="$1" minimum="$2" maximum="$3" value="${!1}"
  [[ "$value" =~ ^[0-9]+$ ]] || pcm_config_error "$key must be an integer"
  [ "$value" -ge "$minimum" ] && [ "$value" -le "$maximum" ] || pcm_config_error "$key must be between $minimum and $maximum"
}

pcm_validate_config() {
  local key path
  for key in PCM_BASE_DIR PCM_STAGING_DIR PCM_STORAGE_MOUNT PCM_ACCOUNT_FINGERPRINT_KEY_FILE PCM_DESTINATION_ROOT PCM_RCLONE_CONFIG PCM_PROTON_RUNNER PCM_PROTON_BIN; do
    path="${!key}"
    [[ "$path" == /* ]] || pcm_config_error "$key must be an absolute path"
  done
  [[ "$PCM_SOURCE_REMOTE" =~ ^[A-Za-z0-9._-]+:$ ]] || pcm_config_error 'PCM_SOURCE_REMOTE must be a bare rclone remote ending in colon'
  [[ "$PCM_SOURCE_ROOT" == /* ]] || pcm_config_error 'PCM_SOURCE_ROOT must be absolute'
  [[ "/$PCM_SOURCE_ROOT/" != *'/../'* && "/$PCM_SOURCE_ROOT/" != *'/./'* ]] || pcm_config_error 'PCM_SOURCE_ROOT cannot contain dot segments'
  [[ "$PCM_DESTINATION_ROOT" == /* ]] || pcm_config_error 'PCM_DESTINATION_ROOT must be an absolute Proton path'
  [[ "${PCM_STAGING_DIR%/}" == "${PCM_BASE_DIR%/}/"* ]] || pcm_config_error 'PCM_STAGING_DIR must be below PCM_BASE_DIR'
  case "$PCM_REQUIRE_MOUNTPOINT" in true|false) ;; *) pcm_config_error 'PCM_REQUIRE_MOUNTPOINT must be true or false' ;; esac
  case "$PCM_DESTINATION_CAPACITY_ACKNOWLEDGED" in true|false) ;; *) pcm_config_error 'PCM_DESTINATION_CAPACITY_ACKNOWLEDGED must be true or false' ;; esac
  [[ "$PCM_RUNTIME_USER" =~ ^[A-Za-z_][A-Za-z0-9_.-]*[$]?$ ]] || pcm_config_error 'PCM_RUNTIME_USER is invalid'

  pcm_integer_between PCM_MIN_FREE_BYTES 0 9223372036854775807
  pcm_integer_between PCM_MIN_FREE_INODES 0 9223372036854775807
  pcm_integer_between PCM_INVENTORY_WORKERS 1 32
  pcm_integer_between PCM_CHECKSUM_WORKERS 1 64
  pcm_integer_between PCM_DOWNLOAD_TRANSFERS 1 32
  pcm_integer_between PCM_DOWNLOAD_CHECKERS 1 64
  pcm_integer_between PCM_MULTI_THREAD_STREAMS 1 8
  pcm_integer_between PCM_LOCAL_VERIFY_WORKERS 1 32
  pcm_integer_between PCM_UPLOAD_WORKERS 1 32
  pcm_integer_between PCM_DESTINATION_VERIFY_WORKERS 1 32
  pcm_integer_between PCM_RCLONE_RETRIES 1 20
  pcm_integer_between PCM_RCLONE_LOW_LEVEL_RETRIES 1 50
  pcm_integer_between PCM_MAX_PHASE_ATTEMPTS 1 10
  [[ "$PCM_RETRY_SLEEP" =~ ^[1-9][0-9]*[smh]$ ]] || pcm_config_error 'PCM_RETRY_SLEEP must be a positive duration such as 30s'
  [[ "$PCM_PROGRESS_INTERVAL" =~ ^[1-9][0-9]*[smh]$ ]] || pcm_config_error 'PCM_PROGRESS_INTERVAL must be a positive duration such as 30s'

  PCM_SOURCE_SPEC="${PCM_SOURCE_REMOTE%:}:${PCM_SOURCE_ROOT#/}"
  [ "$PCM_SOURCE_ROOT" != / ] || PCM_SOURCE_SPEC="$PCM_SOURCE_REMOTE"
  export PCM_SOURCE_SPEC
}
