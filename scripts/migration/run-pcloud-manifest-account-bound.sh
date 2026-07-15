#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
[ -f "$LIB_DIR/config.sh" ] || LIB_DIR="$SCRIPT_DIR/lib"
source "$LIB_DIR/config.sh"
source "$LIB_DIR/checks.sh"

case "${1:-}" in
  account-status|inventory|inventory-recursive|checksums|verify-source|remediate) ;;
  *) printf 'account-bound manifest command is not permitted: %s\n' "${1:-missing}" >&2; exit 64 ;;
esac

# The controller deliberately does not put the raw expectation in its child
# argv. Reload ignored host configuration here and exec the immediate account
# checker so no wrapper, status, event, or evidence process receives it.
unset PCM_CONFIG_LOADED PCM_EXPECTED_PCLOUD_ACCOUNT PCM_EXPECTED_PROTON_ACCOUNT
pcm_load_config
pcm_check_rclone_source
expected_account="$PCM_EXPECTED_PCLOUD_ACCOUNT"
unset PCM_EXPECTED_PCLOUD_ACCOUNT PCM_EXPECTED_PROTON_ACCOUNT
printf '%s\n' "$expected_account" | "$SCRIPT_DIR/pcloud-manifest.py" "$@" --expected-account-stdin \
  --fingerprint-key-file "$PCM_ACCOUNT_FINGERPRINT_KEY_FILE"
