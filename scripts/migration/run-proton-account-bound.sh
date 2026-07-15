#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
[ -f "$LIB_DIR/config.sh" ] || LIB_DIR="$SCRIPT_DIR/lib"
source "$LIB_DIR/config.sh"

unset PCM_CONFIG_LOADED PCM_EXPECTED_PCLOUD_ACCOUNT PCM_EXPECTED_PROTON_ACCOUNT
pcm_load_config
expected_account="$PCM_EXPECTED_PROTON_ACCOUNT"
unset PCM_EXPECTED_PCLOUD_ACCOUNT PCM_EXPECTED_PROTON_ACCOUNT
printf '%s\n' "$expected_account" | "$SCRIPT_DIR/proton-account.py" --proton-run "$PCM_PROTON_RUNNER" --proton-bin "$PCM_PROTON_BIN" \
  --expected-account-stdin --fingerprint-key-file "$PCM_ACCOUNT_FINGERPRINT_KEY_FILE" \
  --expected-version "$PCM_PROTON_CLI_EXPECTED_VERSION" "$@"
