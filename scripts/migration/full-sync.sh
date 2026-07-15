#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
RELEASE_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
LIB_DIR="$SCRIPT_DIR/../lib"
[ -f "$LIB_DIR/config.sh" ] || LIB_DIR="$SCRIPT_DIR/lib"
source "$LIB_DIR/config.sh"
source "$LIB_DIR/safety.sh"
source "$LIB_DIR/checks.sh"
source "$LIB_DIR/runtime.sh"
pcm_load_config

PCM_FULL_SYNC="$SCRIPT_DIR/full-sync.sh"
export PCM_FULL_SYNC
MANIFEST_ROOT="$PCM_BASE_DIR/manifests"
CURRENT_MANIFEST="$MANIFEST_ROOT/current"
MANIFEST_DB="$CURRENT_MANIFEST/inventory.sqlite"
FROZEN_MANIFEST="$CURRENT_MANIFEST/frozen.json"
LIVE_MANIFEST_ROOT="$MANIFEST_ROOT/live-freshness"
MANIFEST_TOOL="$SCRIPT_DIR/pcloud-manifest.py"
ACCOUNT_BOUND_MANIFEST_TOOL="$SCRIPT_DIR/run-pcloud-manifest-account-bound.sh"
PROTON_ACCOUNT_TOOL="$SCRIPT_DIR/proton-account.py"
PROTON_ACCOUNT_BOUND_TOOL="$SCRIPT_DIR/run-proton-account-bound.sh"
PROTON_UPLOAD_TOOL="$SCRIPT_DIR/proton-upload.py"
PROTON_REMEDIATE_TOOL="$SCRIPT_DIR/proton-remediate.py"
PROTON_VERIFY_TOOL="$SCRIPT_DIR/proton-verify.py"
PROTON_STATE="$PCM_BASE_DIR/proton-upload"
PROTON_VERIFY_STATE="$PCM_BASE_DIR/proton-verification"
DESTINATION_ACCOUNT_EVIDENCE="$PCM_BASE_DIR/accounts/destination.json"
SOURCE_ACCOUNT_EVIDENCE="$PCM_BASE_DIR/accounts/source.json"
UPLOAD_ACCEPTED_EVIDENCE="$PROTON_STATE/upload-accepted.json"
FINAL_HANDOFF="$PCM_BASE_DIR/reports/final-handoff.json"
TOOL_VERSIONS="$PCM_BASE_DIR/preflight/tool-versions.json"
PROTON_COMPATIBILITY_EVIDENCE="$PCM_BASE_DIR/preflight/proton-compatibility.json"
PUBLIC_CLI="pcloud-proton-migrate --config $PCM_CONFIG_FILE"

usage() {
  cat <<'EOF'
Usage: full-sync.sh <command>

Compatibility dispatcher. Prefer bin/pcloud-proton-migrate.
  inventory-start | inventory-status | checksums-start | source-freeze
  download-start | metadata-apply-start | reconcile-start
  verify-local-start [none|sha1] | remote-check-start | remediate-start
  phase-status <phase> | status [--json] | recover | audit
  account-status | upload-plan | upload-start | upload-recover-start | upload-status
  proton-verify-start | proton-verify-status | supervise | completion-gate
EOF
}

prepare_state() {
  pcm_validate_storage_layout
  pcm_ensure_run_id
  pcm_check_capacity >/dev/null
  mkdir -p "$PCM_BASE_DIR" "$PCM_STAGING_DIR" "$CURRENT_MANIFEST"
}

runtime_command() {
  if [ "$(id -un)" = "$PCM_RUNTIME_USER" ]; then
    printf '%s\0' "$@"
  elif [ "$(id -u)" -eq 0 ]; then
    printf '%s\0' sudo -u "$PCM_RUNTIME_USER" -H "$@"
  else
    printf 'runtime error: run as %s or root\n' "$PCM_RUNTIME_USER" >&2
    return 2
  fi
}

prepare_runtime_paths() {
  mkdir -p "$@"
  if [ "$(id -u)" -eq 0 ]; then
    chown -R "$PCM_RUNTIME_USER:$(id -gn "$PCM_RUNTIME_USER")" "$@"
  fi
}

phase_recovery() {
  case "$1" in
    inventory) printf '%s source inventory resume' "$PUBLIC_CLI" ;;
    checksums) printf '%s source checksums resume' "$PUBLIC_CLI" ;;
    source-freeze) printf '%s source freeze' "$PUBLIC_CLI" ;;
    download) printf '%s download resume' "$PUBLIC_CLI" ;;
    metadata-apply) printf '%s local metadata resume' "$PUBLIC_CLI" ;;
    reconcile) printf '%s local reconcile resume' "$PUBLIC_CLI" ;;
    verify-local) printf '%s local verify resume' "$PUBLIC_CLI" ;;
    source-freshness) printf '%s source freshness resume' "$PUBLIC_CLI" ;;
    remediate) printf '%s local remediate resume' "$PUBLIC_CLI" ;;
    upload|upload-plan|upload-acceptance) printf '%s upload resume' "$PUBLIC_CLI" ;;
    proton-verify|destination-verification) printf '%s destination verify resume' "$PUBLIC_CLI" ;;
    *) printf '%s supervisor-run' "$PUBLIC_CLI" ;;
  esac
}

require_phase_complete() {
  local phase="$1"
  [ "$(phase_state "$phase")" = complete ] || {
    printf 'lifecycle gate: %s must be complete; use %s\n' "$phase" "$(phase_recovery "$phase")" >&2
    return 2
  }
}

require_local_acceptance() {
  local report="$PCM_BASE_DIR/reports/download-completion-audit.json"
  audit >/dev/null
  jq -e '
    . as $report |
    ($report.status == "complete" or $report.status == "verified") and
    (["missing","unexpected","type","size","sha1","mtime"] | all((($report.mismatch_classes[.] // $report[.]) | type) == "number" and ($report.mismatch_classes[.] // $report[.]) == 0)) and
    (($report.files_verified // $report.files_complete) | type) == "number" and ($report.files_expected | type) == "number" and
    ($report.files_verified // $report.files_complete) == $report.files_expected and
    (($report.directories_verified // $report.directories_complete) | type) == "number" and ($report.directories_expected | type) == "number" and
    ($report.directories_verified // $report.directories_complete) == $report.directories_expected and
    (($report.sha1_verified // $report.sha1_complete) | type) == "number" and ($report.sha1_expected | type) == "number" and
    ($report.sha1_verified // $report.sha1_complete) == $report.sha1_expected
  ' "$report" >/dev/null || {
    printf 'local reconciliation/SHA1 acceptance gate failed\n' >&2
    return 2
  }
}

verify_frozen_manifest() {
  local actual
  [ -f "$FROZEN_MANIFEST" ] || { printf 'manifest is not frozen; run the public source freeze command\n' >&2; return 2; }
  actual="$("$MANIFEST_TOOL" snapshot --db "$MANIFEST_DB")"
  jq -e --argjson actual "$actual" '
    .status == "frozen" and
    .snapshot.snapshot_id == $actual.snapshot.snapshot_id and
    .snapshot.snapshot_digest == $actual.snapshot.snapshot_digest and
    .snapshot.snapshot_digest_algorithm == $actual.snapshot.snapshot_digest_algorithm and
    .snapshot.snapshot_generation == $actual.snapshot.snapshot_generation and
    .snapshot.frozen_at == $actual.snapshot.frozen_at and
    .snapshot.source_account_fingerprint == $actual.snapshot.source_account_fingerprint
  ' "$FROZEN_MANIFEST" >/dev/null || {
    printf 'frozen manifest does not match its immutable snapshot/account binding\n' >&2
    return 2
  }
}

source_fingerprint() {
  jq -er '.snapshot.source_account_fingerprint' "$FROZEN_MANIFEST"
}

destination_fingerprint() {
  jq -er '.destination_account_fingerprint' "$DESTINATION_ACCOUNT_EVIDENCE"
}

tool_versions_json() {
  local toolkit bundle python_version rclone_version proton_version compatible
  toolkit="$(awk -F= '$1 ~ /^[[:space:]]*version[[:space:]]*$/ {gsub(/[[:space:]\"]/, "", $2); print $2; exit}' "$RELEASE_ROOT/pyproject.toml")"
  bundle=""
  [ ! -f "$RELEASE_ROOT/BUNDLE-SHA256SUMS" ] || bundle="$(sha256sum "$RELEASE_ROOT/BUNDLE-SHA256SUMS" | awk '{print $1}')"
  python_version="$(python3 --version 2>&1 | head -n 1 || true)"
  rclone_version="$(rclone version 2>&1 | head -n 1 || true)"
  proton_version="$(jq -r '.proton_version // empty' "$DESTINATION_ACCOUNT_EVIDENCE" 2>/dev/null || true)"
  compatible="$(jq -r '.version_compatible // false' "$DESTINATION_ACCOUNT_EVIDENCE" 2>/dev/null || printf false)"
  jq -cn --arg toolkit "$toolkit" --arg bundle_sha256 "$bundle" --arg python "$python_version" \
    --arg rclone "$rclone_version" --arg proton "$proton_version" --arg expected_proton "$PCM_PROTON_CLI_EXPECTED_VERSION" --argjson compatible "$compatible" \
    '{toolkit:$toolkit,bundle_sha256:$bundle_sha256,python:$python,rclone:$rclone,proton_version:$proton,expected_proton_version:$expected_proton,version_compatible:$compatible}'
}

persist_tool_versions() {
  local candidate
  candidate="$(tool_versions_json)"
  jq -e '([.toolkit,.bundle_sha256,.python,.rclone,.proton_version,.expected_proton_version] | all(type == "string" and length > 0)) and .version_compatible == true' <<<"$candidate" >/dev/null || {
    printf 'version evidence is incomplete or incompatible\n' >&2
    return 2
  }
  if [ -f "$TOOL_VERSIONS" ]; then
    jq -e --argjson candidate "$candidate" '. == $candidate' "$TOOL_VERSIONS" >/dev/null || {
      printf 'persisted version tuple differs from the current toolchain\n' >&2
      return 2
    }
  else
    jq -c . <<<"$candidate" | pcm_atomic_write "$TOOL_VERSIONS"
  fi
  cat "$TOOL_VERSIONS"
}

write_phase_binding() {
  local phase="$1" dir destination=null versions
  verify_frozen_manifest
  dir="$(pcm_phase_dir "$phase")"
  mkdir -p "$dir"
  if [ -f "$DESTINATION_ACCOUNT_EVIDENCE" ]; then
    destination="$(destination_fingerprint)"
  fi
  versions="$(persist_tool_versions)"
  jq -cn --arg at "$(pcm_now)" \
    --arg snapshot_id "$(jq -er '.snapshot.snapshot_id' "$FROZEN_MANIFEST")" \
    --arg snapshot_digest "$(jq -er '.snapshot.snapshot_digest' "$FROZEN_MANIFEST")" \
    --arg snapshot_digest_algorithm "$(jq -er '.snapshot.snapshot_digest_algorithm' "$FROZEN_MANIFEST")" \
    --argjson snapshot_generation "$(jq -er '.snapshot.snapshot_generation' "$FROZEN_MANIFEST")" \
    --arg frozen_at "$(jq -er '.snapshot.frozen_at' "$FROZEN_MANIFEST")" \
    --arg source_account_fingerprint "$(source_fingerprint)" \
    --arg destination_account_fingerprint "$destination" --argjson versions "$versions" \
    '{bound_at:$at,snapshot_id:$snapshot_id,snapshot_digest:$snapshot_digest,snapshot_digest_algorithm:$snapshot_digest_algorithm,snapshot_generation:$snapshot_generation,frozen_at:$frozen_at,source_account_fingerprint:$source_account_fingerprint,destination_account_fingerprint:(if $destination_account_fingerprint == "null" then null else $destination_account_fingerprint end),versions:$versions}' |
    pcm_atomic_write "$dir/binding.json"
}

source_account_refresh() {
  local output
  pcm_validate_storage_layout
  pcm_check_rclone_source
  prepare_runtime_paths "$(dirname "$SOURCE_ACCOUNT_EVIDENCE")"
  output="$("$ACCOUNT_BOUND_MANIFEST_TOOL" account-status --remote "$PCM_SOURCE_SPEC" --rclone-config "$PCM_RCLONE_CONFIG")"
  jq -e '.kind == "pcloud_account" and .status == "verified" and (.generated_at | type) == "string" and (.source_account_fingerprint | type == "string" and length == 64) and (.quota.quota_bytes | type) == "number" and (.quota.used_bytes | type) == "number"' <<<"$output" >/dev/null || { printf 'source account fingerprint evidence is invalid\n' >&2; return 2; }
  jq -c . <<<"$output" | pcm_atomic_write "$SOURCE_ACCOUNT_EVIDENCE"
}

source_account_status() {
  source_account_refresh
  jq -c . "$SOURCE_ACCOUNT_EVIDENCE"
}

inventory_start() {
  prepare_state
  source_account_refresh
  compatibility_status >/dev/null
  [ ! -f "$FROZEN_MANIFEST" ] || { printf 'source manifest is frozen; use a new base directory for a new snapshot\n' >&2; return 2; }
  pcm_start_phase inventory "$(phase_recovery inventory)" "$ACCOUNT_BOUND_MANIFEST_TOOL" inventory \
    --db "$MANIFEST_DB" --progress "$(pcm_phase_dir inventory)/progress.json" \
    --remote "$PCM_SOURCE_SPEC" --rclone-config "$PCM_RCLONE_CONFIG" \
    --workers "$PCM_INVENTORY_WORKERS" --retries "$PCM_RCLONE_RETRIES" --transport api --retry-failed
}

inventory_status() {
  pcm_check_rclone_source
  if [ -f "$MANIFEST_DB" ]; then "$MANIFEST_TOOL" status --db "$MANIFEST_DB"; else printf '{"kind":"source_inventory","status":"not_started"}\n'; fi
}

checksums_start() {
  prepare_state
  pcm_check_rclone_source
  [ -f "$MANIFEST_DB" ] || { printf 'inventory is missing\n' >&2; return 2; }
  require_phase_complete inventory
  [ ! -f "$FROZEN_MANIFEST" ] || { printf 'source manifest is frozen; checksum enrichment is forbidden\n' >&2; return 2; }
  pcm_start_phase checksums "$(phase_recovery checksums)" "$ACCOUNT_BOUND_MANIFEST_TOOL" checksums \
    --db "$MANIFEST_DB" --progress "$(pcm_phase_dir checksums)/progress.json" \
    --rclone-config "$PCM_RCLONE_CONFIG" \
    --workers "$PCM_CHECKSUM_WORKERS" --retries "$PCM_RCLONE_RETRIES" --retry-failed
}

audit() {
  mkdir -p "$PCM_BASE_DIR/reports"
  "$MANIFEST_TOOL" completion-audit --db "$MANIFEST_DB" --operations "$(pcm_operation_root)" \
    --report "$PCM_BASE_DIR/reports/download-completion-audit.json"
}

source_freeze() {
  local code=0 recovery
  prepare_state
  pcm_check_rclone_source
  [ -f "$MANIFEST_DB" ] || { printf 'inventory is missing\n' >&2; return 2; }
  require_phase_complete inventory
  require_phase_complete checksums
  recovery="$(phase_recovery source-freeze)"
  mkdir -p "$(pcm_phase_dir source-freeze)" "$(pcm_operation_root)/source-freeze"
  ln -sfn "$(pcm_phase_dir source-freeze)" "$(pcm_operation_root)/source-freeze/current"
  pcm_write_phase_status source-freeze running "$recovery"
  if "$MANIFEST_TOOL" freeze --db "$MANIFEST_DB" --report "$FROZEN_MANIFEST" >/dev/null && verify_frozen_manifest; then
    write_phase_binding inventory
    write_phase_binding checksums
    write_phase_binding source-freeze
    archive_attention source-freeze
    pcm_write_phase_status source-freeze complete "$recovery"
    pcm_event source-freeze frozen "snapshot_id=$(jq -r '.snapshot.snapshot_id' "$FROZEN_MANIFEST")"
    cat "$FROZEN_MANIFEST"
  else
    code=$?
    pcm_write_phase_status source-freeze failed "$recovery" "$code"
    pcm_event source-freeze failed "exit_code=$code" phase_failure
    pcm_write_attention source-freeze 'source freeze failed' "$recovery" phase_failure
    return "$code"
  fi
}

download_start() {
  prepare_state
  pcm_check_rclone_source
  verify_frozen_manifest
  require_phase_complete source-freeze
  write_phase_binding download
  pcm_start_phase download "$(phase_recovery download)" env PCM_OPERATION_DIR="$(pcm_phase_dir download)" "$SCRIPT_DIR/full-pcloud-download.sh"
}

metadata_apply_start() {
  prepare_state
  require_phase_complete download
  write_phase_binding metadata-apply
  pcm_start_phase metadata-apply "$(phase_recovery metadata-apply)" "$MANIFEST_TOOL" apply-metadata \
    --db "$MANIFEST_DB" --stage "$PCM_STAGING_DIR" --progress "$(pcm_phase_dir metadata-apply)/progress.json"
}

reconcile_start() {
  prepare_state
  require_phase_complete metadata-apply
  write_phase_binding reconcile
  pcm_start_phase reconcile "$(phase_recovery reconcile)" "$MANIFEST_TOOL" reconcile-summary \
    --db "$MANIFEST_DB" --stage "$PCM_STAGING_DIR" --report "$(pcm_phase_dir reconcile)/report.json" \
    --progress "$(pcm_phase_dir reconcile)/progress.json"
}

verify_local_start() {
  local hash_mode="${1:-sha1}"
  case "$hash_mode" in none|sha1) ;; *) printf 'hash mode must be none or sha1\n' >&2; return 2 ;; esac
  prepare_state
  require_phase_complete reconcile
  write_phase_binding verify-local
  pcm_start_phase verify-local "$(phase_recovery verify-local)" "$MANIFEST_TOOL" verify-local \
    --db "$MANIFEST_DB" --stage "$PCM_STAGING_DIR" --report "$(pcm_phase_dir verify-local)/report.json" \
    --progress "$(pcm_phase_dir verify-local)/progress.json" --hash "$hash_mode" \
    --workers "$PCM_LOCAL_VERIFY_WORKERS" --detect-extras --resume
}

remote_check_start() {
  local live_db
  prepare_state
  pcm_check_rclone_source
  require_phase_complete verify-local
  write_phase_binding source-freshness
  live_db="$LIVE_MANIFEST_ROOT/$PCM_RUN_ID/inventory.sqlite"
  mkdir -p "$(dirname "$live_db")"
  pcm_start_phase source-freshness "$(phase_recovery source-freshness)" "$ACCOUNT_BOUND_MANIFEST_TOOL" verify-source \
    --db "$MANIFEST_DB" --live-db "$live_db" \
    --report "$(pcm_phase_dir source-freshness)/report.json" --progress "$(pcm_phase_dir source-freshness)/progress.json" \
    --remote "$PCM_SOURCE_SPEC" --rclone-config "$PCM_RCLONE_CONFIG" --workers 2
}

remediate_start() {
  local verify_dir mismatch
  verify_dir="$(pcm_current_phase_dir verify-local)"
  mismatch="$verify_dir/report.mismatches.jsonl"
  [ -f "$mismatch" ] || { printf 'local verification mismatch report is missing\n' >&2; return 2; }
  prepare_state
  pcm_check_rclone_source
  write_phase_binding remediate
  pcm_start_phase remediate "$(phase_recovery remediate)" "$ACCOUNT_BOUND_MANIFEST_TOOL" remediate \
    --db "$MANIFEST_DB" --stage "$PCM_STAGING_DIR" --mismatches "$mismatch" \
    --progress "$(pcm_phase_dir remediate)/progress.json" --remote "$PCM_SOURCE_SPEC" \
    --rclone-config "$PCM_RCLONE_CONFIG" --workers "$PCM_DOWNLOAD_TRANSFERS" \
    --retries "$PCM_RCLONE_RETRIES" --low-level-retries "$PCM_RCLONE_LOW_LEVEL_RETRIES"
}

destination_account_refresh() {
  local -a command
  local output
  pcm_validate_storage_layout
  prepare_runtime_paths "$(dirname "$DESTINATION_ACCOUNT_EVIDENCE")"
  mapfile -d '' -t command < <(runtime_command "$PROTON_ACCOUNT_BOUND_TOOL")
  output="$("${command[@]}")"
  jq -e --arg expected "$PCM_PROTON_CLI_EXPECTED_VERSION" '.kind == "proton_account" and .status == "verified" and (.generated_at | type) == "string" and (.destination_account_fingerprint | type == "string" and length == 64) and .proton_version == $expected and .version_compatible == true and ((.quota.quota_bytes == null) or ((.quota.quota_bytes | type) == "number")) and ((.quota.used_bytes == null) or ((.quota.used_bytes | type) == "number"))' <<<"$output" >/dev/null || {
    printf 'destination account fingerprint evidence is invalid\n' >&2
    return 2
  }
  jq -c . <<<"$output" | pcm_atomic_write "$DESTINATION_ACCOUNT_EVIDENCE"
}

account_status() {
  destination_account_refresh
  jq -c . "$DESTINATION_ACCOUNT_EVIDENCE"
}

compatibility_status() {
  local -a command
  local filesystem probe_code result cache
  destination_account_refresh
  persist_tool_versions >/dev/null
  cache="$PCM_BASE_DIR/preflight/proton-compatibility-cache"
  prepare_runtime_paths "$cache"
  mapfile -d '' -t command < <(runtime_command "$PROTON_VERIFY_TOOL" probe-filesystem-interface \
    --destination "$PCM_DESTINATION_ROOT" --cache "$cache" \
    --proton-run "$PCM_PROTON_RUNNER" --proton-bin "$PCM_PROTON_BIN")
  if filesystem="$("${command[@]}")"; then
    probe_code=0
  else
    probe_code=$?
  fi
  if ! jq -e '.kind == "proton_filesystem_interface_probe" and (.filesystem_interface_compatible | type) == "boolean"' <<<"$filesystem" >/dev/null 2>&1; then
    filesystem='{"kind":"proton_filesystem_interface_probe","status":"incompatible","filesystem_interface_compatible":false,"error_class":"interface-incompatible"}'
    probe_code=2
  fi
  result="$(jq -cn --arg expected "$PCM_PROTON_CLI_EXPECTED_VERSION" \
    --argjson account "$(cat "$DESTINATION_ACCOUNT_EVIDENCE")" --argjson filesystem "$filesystem" '
    {kind:"proton_compatibility_probe",status:(if ($account.version_compatible and $filesystem.filesystem_interface_compatible) then "compatible" else "incompatible" end),generated_at:$account.generated_at,destination_account_fingerprint:$account.destination_account_fingerprint,proton_version:$account.proton_version,expected_proton_version:$expected,version_compatible:$account.version_compatible,filesystem_interface_compatible:$filesystem.filesystem_interface_compatible,filesystem_interface:$filesystem}')"
  jq -c . <<<"$result" | pcm_atomic_write "$PROTON_COMPATIBILITY_EVIDENCE"
  printf '%s\n' "$result"
  return "$probe_code"
}

check_destination_capacity() {
  local required quota used available
  required="$(jq -r '.snapshot.bytes // .snapshot.total_bytes // .totals.bytes // empty' "$FROZEN_MANIFEST")"
  quota="$(jq -r '.quota.quota_bytes // ""' "$DESTINATION_ACCOUNT_EVIDENCE")"
  used="$(jq -r '.quota.used_bytes // ""' "$DESTINATION_ACCOUNT_EVIDENCE")"
  if [[ "$required" =~ ^[0-9]+$ && "$quota" =~ ^[0-9]+$ && "$used" =~ ^[0-9]+$ ]]; then
    available=$((quota - used))
    [ "$available" -ge "$required" ] || {
      printf 'destination capacity is below frozen snapshot bytes\n' >&2
      return 75
    }
    return 0
  fi
  [ "$PCM_DESTINATION_CAPACITY_ACKNOWLEDGED" = true ] || {
    printf 'destination quota is unavailable and capacity is not explicitly acknowledged\n' >&2
    return 75
  }
}

upload_plan() {
  local -a command
  local fingerprint
  prepare_state
  verify_frozen_manifest
  require_phase_complete source-freshness
  require_phase_complete verify-local
  require_local_acceptance
  destination_account_refresh
  check_destination_capacity
  fingerprint="$(destination_fingerprint)"
  prepare_runtime_paths "$PROTON_STATE"
  write_phase_binding upload-plan
  cp "$(pcm_phase_dir upload-plan)/binding.json" "$PROTON_STATE/binding.json"
  mapfile -d '' -t command < <(runtime_command "$PROTON_UPLOAD_TOOL" plan --db "$PROTON_STATE/upload.sqlite" \
    --manifest "$MANIFEST_DB" --stage "$PCM_STAGING_DIR" --destination "$PCM_DESTINATION_ROOT" \
    --destination-account-fingerprint "$fingerprint" --progress "$PROTON_STATE/progress.json")
  "${command[@]}"
  archive_attention upload-plan
}

upload_start() {
  local -a command
  local fingerprint
  prepare_state
  verify_frozen_manifest
  require_phase_complete source-freshness
  require_local_acceptance
  destination_account_refresh
  check_destination_capacity
  fingerprint="$(destination_fingerprint)"
  [ -f "$PROTON_STATE/upload.sqlite" ] || { printf 'upload plan is missing; run the public upload plan command\n' >&2; return 2; }
  write_phase_binding upload
  prepare_runtime_paths "$PROTON_STATE" "$(pcm_phase_dir upload)"
  mapfile -d '' -t command < <(runtime_command "$PROTON_UPLOAD_TOOL" run --db "$PROTON_STATE/upload.sqlite" \
    --progress "$(pcm_phase_dir upload)/progress.json" --logs "$PROTON_STATE/logs" --cache "$PROTON_STATE/cache" \
    --proton-run "$PCM_PROTON_RUNNER" --proton-bin "$PCM_PROTON_BIN" \
    --destination-account-fingerprint "$fingerprint" --workers "$PCM_UPLOAD_WORKERS" \
    --max-attempts "$PCM_MAX_PHASE_ATTEMPTS")
  pcm_start_phase upload "$(phase_recovery upload)" "${command[@]}"
}

upload_recover_start() {
  local -a command
  local fingerprint
  prepare_state
  verify_frozen_manifest
  require_phase_complete source-freshness
  [ -f "$PROTON_STATE/upload.sqlite" ] || { printf 'upload plan is missing\n' >&2; return 2; }
  require_local_acceptance
  destination_account_refresh
  fingerprint="$(destination_fingerprint)"
  write_phase_binding upload
  prepare_runtime_paths "$PROTON_STATE" "$(pcm_phase_dir upload)"
  mapfile -d '' -t command < <(runtime_command bash -c '
    set -Eeuo pipefail
    "$1" --db "$2" --manifest "$3" --cache "$4" --temp "$5" --evidence "$6" --proton-run "$7" --proton-bin "$8" --max-attempts "${13}" --destination-account-fingerprint "${14}" || true
    exec "$9" run --db "$2" --progress "${10}" --logs "${11}" --cache "$4" --proton-run "$7" --proton-bin "$8" --workers "${12}" --max-attempts "${13}" --destination-account-fingerprint "${14}" --retry-failed
  ' bash "$PROTON_REMEDIATE_TOOL" "$PROTON_STATE/upload.sqlite" "$MANIFEST_DB" "$PROTON_STATE/cache" \
    "$PROTON_STATE/remediation-temp" "$PROTON_STATE/remediation-evidence.json" "$PCM_PROTON_RUNNER" "$PCM_PROTON_BIN" \
    "$PROTON_UPLOAD_TOOL" "$(pcm_phase_dir upload)/progress.json" "$PROTON_STATE/logs" "$PCM_UPLOAD_WORKERS" "$PCM_MAX_PHASE_ATTEMPTS" "$fingerprint")
  pcm_start_phase upload "$(phase_recovery upload)" "${command[@]}"
}

upload_status() {
  if [ -f "$PROTON_STATE/upload.sqlite" ] && [ -f "$DESTINATION_ACCOUNT_EVIDENCE" ]; then
    "$PROTON_UPLOAD_TOOL" status --db "$PROTON_STATE/upload.sqlite" --destination-account-fingerprint "$(destination_fingerprint)"
  else
    printf '{"kind":"proton_upload","status":"not_started"}\n'
  fi
}

write_upload_acceptance() {
  local status binding
  status="$(upload_status)"
  binding="$PROTON_STATE/binding.json"
  [ -f "$binding" ] || { printf 'upload snapshot/account binding is missing\n' >&2; return 2; }
  jq -e --slurpfile binding "$binding" '
    .kind == "proton_upload" and .status == "complete" and .upload_acceptance == "accepted" and
    .account_binding_satisfied == true and
    ([.units_complete,.units_expected,.files_complete,.files_expected,.bytes_complete,.bytes_expected,.remote_dirs_complete,.remote_dirs_expected] | all(type == "number")) and
    (.units_complete == .units_expected) and (.files_complete == .files_expected) and
    (.bytes_complete == .bytes_expected) and (.remote_dirs_complete == .remote_dirs_expected) and
    (.units_failed | type == "number" and . == 0) and
    (.remote_dirs_failed | type == "number" and . == 0) and
    (.attempts_exhausted | type == "number" and . == 0) and
    (.units_pending | type == "number" and . == 0) and
    (.units_running | type == "number" and . == 0) and
    (.remote_dirs_pending | type == "number" and . == 0) and
    (.remote_dirs_running | type == "number" and . == 0) and
    (.auth_failures | type == "number" and . == 0) and
    (.quota_failures | type == "number" and . == 0) and
    (.error_failures | type == "number" and . == 0) and
    .snapshot.snapshot_id == $binding[0].snapshot_id and
    .snapshot.snapshot_digest == $binding[0].snapshot_digest and
    .snapshot.snapshot_digest_algorithm == $binding[0].snapshot_digest_algorithm and
    .snapshot.snapshot_generation == $binding[0].snapshot_generation and
    .snapshot.frozen_at == $binding[0].frozen_at and
    .source_account_fingerprint == $binding[0].source_account_fingerprint and
    .destination_account_fingerprint == $binding[0].destination_account_fingerprint
  ' <<<"$status" >/dev/null || { printf 'upload acceptance requirements are not satisfied\n' >&2; return 2; }
  jq -c . <<<"$status" | pcm_atomic_write "$UPLOAD_ACCEPTED_EVIDENCE"
  archive_attention upload-acceptance
}

proton_verify_start() {
  local -a command retry=()
  local fingerprint
  prepare_state
  verify_frozen_manifest
  audit >/dev/null
  [ -f "$UPLOAD_ACCEPTED_EVIDENCE" ] || { printf 'upload-accepted evidence is required before destination verification\n' >&2; return 2; }
  jq -e '.status == "complete" and .upload_acceptance == "accepted"' "$UPLOAD_ACCEPTED_EVIDENCE" >/dev/null || { printf 'upload acceptance evidence is invalid\n' >&2; return 2; }
  destination_account_refresh
  fingerprint="$(destination_fingerprint)"
  [ ! -f "$PROTON_VERIFY_STATE/verify.sqlite" ] || retry+=(--retry-failed --resume)
  write_phase_binding proton-verify
  prepare_runtime_paths "$PROTON_VERIFY_STATE" "$(pcm_phase_dir proton-verify)"
  mapfile -d '' -t command < <(runtime_command "$PROTON_VERIFY_TOOL" run --db "$PROTON_VERIFY_STATE/verify.sqlite" \
    --manifest "$MANIFEST_DB" --destination "$PCM_DESTINATION_ROOT" \
    --destination-account-fingerprint "$fingerprint" --upload-evidence "$UPLOAD_ACCEPTED_EVIDENCE" \
    --progress "$(pcm_phase_dir proton-verify)/progress.json" --cache "$PROTON_VERIFY_STATE/cache" \
    --proton-run "$PCM_PROTON_RUNNER" --proton-bin "$PCM_PROTON_BIN" \
    --workers "$PCM_DESTINATION_VERIFY_WORKERS" --max-attempts "$PCM_MAX_PHASE_ATTEMPTS" "${retry[@]}")
  pcm_start_phase proton-verify "$(phase_recovery proton-verify)" "${command[@]}"
}

proton_verify_status() {
  if [ -f "$PROTON_VERIFY_STATE/verify.sqlite" ] && [ -f "$DESTINATION_ACCOUNT_EVIDENCE" ]; then
    "$PROTON_VERIFY_TOOL" status --db "$PROTON_VERIFY_STATE/verify.sqlite" --destination-account-fingerprint "$(destination_fingerprint)"
  else
    printf '{"kind":"proton_verification","status":"not_started"}\n'
  fi
}

overall_json() {
  local temporary phase freshness_dir
  temporary="$(mktemp -d "${TMPDIR:-/tmp}/pcm-status.XXXXXX")"
  trap 'rm -rf "$temporary"' RETURN
  for phase in inventory checksums source-freeze download metadata-apply reconcile verify-local source-freshness remediate upload proton-verify; do
    pcm_phase_status_json "$phase" > "$temporary/$phase.json"
  done
  upload_status > "$temporary/upload-provider.json"
  proton_verify_status > "$temporary/destination-provider.json"
  inventory_status > "$temporary/source-inventory.json"
  [ ! -f "$FROZEN_MANIFEST" ] || cp "$FROZEN_MANIFEST" "$temporary/source-snapshot.json"
  [ -f "$temporary/source-snapshot.json" ] || printf 'null\n' > "$temporary/source-snapshot.json"
  [ ! -f "$PCM_BASE_DIR/reports/download-completion-audit.json" ] || cp "$PCM_BASE_DIR/reports/download-completion-audit.json" "$temporary/local-audit.json"
  [ -f "$temporary/local-audit.json" ] || printf 'null\n' > "$temporary/local-audit.json"
  [ ! -f "$PCM_BASE_DIR/ATTENTION_REQUIRED.json" ] || cp "$PCM_BASE_DIR/ATTENTION_REQUIRED.json" "$temporary/attention.json"
  [ -f "$temporary/attention.json" ] || printf 'null\n' > "$temporary/attention.json"
  [ ! -f "$DESTINATION_ACCOUNT_EVIDENCE" ] || jq . "$DESTINATION_ACCOUNT_EVIDENCE" > "$temporary/account.json"
  [ -f "$temporary/account.json" ] || printf 'null\n' > "$temporary/account.json"
  [ ! -f "$UPLOAD_ACCEPTED_EVIDENCE" ] || cp "$UPLOAD_ACCEPTED_EVIDENCE" "$temporary/acceptance.json"
  [ -f "$temporary/acceptance.json" ] || printf 'null\n' > "$temporary/acceptance.json"
  [ ! -f "$TOOL_VERSIONS" ] || cp "$TOOL_VERSIONS" "$temporary/versions.json"
  [ -f "$temporary/versions.json" ] || printf 'null\n' > "$temporary/versions.json"
  [ ! -f "$PROTON_COMPATIBILITY_EVIDENCE" ] || cp "$PROTON_COMPATIBILITY_EVIDENCE" "$temporary/compatibility.json"
  [ -f "$temporary/compatibility.json" ] || printf 'null\n' > "$temporary/compatibility.json"
  [ ! -f "$SOURCE_ACCOUNT_EVIDENCE" ] || cp "$SOURCE_ACCOUNT_EVIDENCE" "$temporary/source-account.json"
  [ -f "$temporary/source-account.json" ] || printf 'null\n' > "$temporary/source-account.json"
  freshness_dir="$(pcm_current_phase_dir source-freshness)"
  [ -z "$freshness_dir" ] || [ ! -f "$freshness_dir/report.json" ] || cp "$freshness_dir/report.json" "$temporary/source-freshness-report.json"
  [ -f "$temporary/source-freshness-report.json" ] || printf 'null\n' > "$temporary/source-freshness-report.json"
  jq -n --arg at "$(pcm_now)" --arg run_id "$(pcm_existing_run_id)" --arg source_remote "$PCM_SOURCE_REMOTE" \
    --arg source_root "$PCM_SOURCE_ROOT" --arg staging "$PCM_STAGING_DIR" --arg events "$(pcm_events_file)" \
    --argjson capacity_ack "$PCM_DESTINATION_CAPACITY_ACKNOWLEDGED" \
    --slurpfile inventory "$temporary/inventory.json" --slurpfile checksums "$temporary/checksums.json" \
    --slurpfile freeze "$temporary/source-freeze.json" --slurpfile download "$temporary/download.json" \
    --slurpfile metadata "$temporary/metadata-apply.json" --slurpfile reconcile "$temporary/reconcile.json" \
    --slurpfile local_verify "$temporary/verify-local.json" --slurpfile freshness "$temporary/source-freshness.json" \
    --slurpfile remediate "$temporary/remediate.json" --slurpfile upload_phase "$temporary/upload.json" \
    --slurpfile destination_phase "$temporary/proton-verify.json" --slurpfile upload "$temporary/upload-provider.json" \
    --slurpfile destination "$temporary/destination-provider.json" --slurpfile source_inventory "$temporary/source-inventory.json" \
    --slurpfile source_snapshot "$temporary/source-snapshot.json" --slurpfile local_audit "$temporary/local-audit.json" \
    --slurpfile attention "$temporary/attention.json" --slurpfile account "$temporary/account.json" --slurpfile acceptance "$temporary/acceptance.json" \
    --slurpfile versions "$temporary/versions.json" --slurpfile compatibility_evidence "$temporary/compatibility.json" --slurpfile source_account "$temporary/source-account.json" \
    --slurpfile source_freshness "$temporary/source-freshness-report.json" '
    def numeric_fields($o;$names): $names | all(($o[.] | type) == "number");
    ($source_snapshot[0]) as $snapshot | ($local_audit[0]) as $local | ($upload[0]) as $up |
    ($destination[0]) as $dest | ($acceptance[0]) as $accepted | ($account[0]) as $acct | ($source_account[0]) as $source_acct |
    ($source_inventory[0]) as $source_counts | ($local) as $local_counts | ($dest) as $dest_counts |
    ([$snapshot.snapshot.snapshot_id,$local.snapshot.snapshot_id,$source_freshness[0].snapshot.snapshot_id,$accepted.snapshot.snapshot_id,$dest.snapshot.snapshot_id]) as $ids |
    ([$snapshot.snapshot.snapshot_digest,$local.snapshot.snapshot_digest,$source_freshness[0].snapshot.snapshot_digest,$accepted.snapshot.snapshot_digest,$dest.snapshot.snapshot_digest]) as $digests |
    ([$snapshot.snapshot.snapshot_digest_algorithm,$local.snapshot.snapshot_digest_algorithm,$source_freshness[0].snapshot.snapshot_digest_algorithm,$accepted.snapshot.snapshot_digest_algorithm,$dest.snapshot.snapshot_digest_algorithm]) as $algorithms |
    ([$snapshot.snapshot.snapshot_generation,$local.snapshot.snapshot_generation,$source_freshness[0].snapshot.snapshot_generation,$accepted.snapshot.snapshot_generation,$dest.snapshot.snapshot_generation]) as $generations |
    ([$snapshot.snapshot.frozen_at,$local.snapshot.frozen_at,$source_freshness[0].snapshot.frozen_at,$accepted.snapshot.frozen_at,$dest.snapshot.frozen_at]) as $frozen_times |
    ([$source_acct.source_account_fingerprint,$snapshot.snapshot.source_account_fingerprint,$local.snapshot.source_account_fingerprint,$source_freshness[0].source_account_fingerprint,$accepted.source_account_fingerprint,$dest.source_account_fingerprint]) as $source_fps |
    ([$acct.destination_account_fingerprint,$accepted.destination_account_fingerprint,$dest.destination_account_fingerprint]) as $destination_fps |
    ([$inventory[0].snapshot_binding,$checksums[0].snapshot_binding,$freeze[0].snapshot_binding,$download[0].snapshot_binding,$metadata[0].snapshot_binding,$reconcile[0].snapshot_binding,$local_verify[0].snapshot_binding,$freshness[0].snapshot_binding,$upload_phase[0].snapshot_binding,$destination_phase[0].snapshot_binding]) as $bindings |
    ($compatibility_evidence[0]) as $compatibility |
    ({source_snapshot_frozen:($snapshot.kind == "pcloud_source_snapshot" and $snapshot.status == "frozen" and (["snapshot_id","snapshot_digest","snapshot_digest_algorithm","snapshot_generation","frozen_at","source_account_fingerprint"] | all($snapshot.snapshot[.] != null))),
      source_inventory_complete:($source_counts.status == "complete" and numeric_fields($source_counts;["directories_discovered","directories_complete","directories_pending","directories_running","directories_failed","files_indexed","directory_entries_indexed","bytes_indexed","files_with_sha1","ambiguous_entries","unreadable_names"]) and $source_counts.directories_complete == $source_counts.directories_discovered and $source_counts.directories_pending == 0 and $source_counts.directories_running == 0 and $source_counts.directories_failed == 0 and $source_counts.files_with_sha1 == $source_counts.files_indexed and $source_counts.ambiguous_entries == 0 and $source_counts.unreadable_names == 0),
      source_fresh:($source_freshness[0].status == "fresh" and ($source_freshness[0].differences | type) == "number" and $source_freshness[0].differences == 0 and (["missing_from_live","new_in_live","metadata_or_content_changed"] | all(($source_freshness[0].counts[.] | type) == "number" and $source_freshness[0].counts[.] == 0))),
      local_verified:($local.status == "complete" and numeric_fields($local_counts;["files_expected","files_verified","directories_expected","directories_verified","bytes_expected","bytes_verified","sha1_expected","sha1_verified"]) and (["missing","unexpected","type","size","sha1","mtime"] | all(($local_counts.mismatch_classes[.] | type) == "number" and $local_counts.mismatch_classes[.] == 0)) and $local_counts.files_verified == $local_counts.files_expected and $local_counts.directories_verified == $local_counts.directories_expected and $local_counts.bytes_verified == $local_counts.bytes_expected and $local_counts.sha1_verified == $local_counts.sha1_expected),
      upload_accepted:($accepted.status == "complete" and $accepted.upload_acceptance == "accepted" and $accepted.account_binding_satisfied == true and numeric_fields($accepted;["units_expected","units_complete","units_pending","units_running","units_failed","files_expected","files_complete","bytes_expected","bytes_complete","remote_dirs_expected","remote_dirs_complete","remote_dirs_pending","remote_dirs_running","remote_dirs_failed","attempts_exhausted","auth_failures","quota_failures","error_failures"]) and $accepted.units_complete == $accepted.units_expected and $accepted.files_complete == $accepted.files_expected and $accepted.bytes_complete == $accepted.bytes_expected and $accepted.remote_dirs_complete == $accepted.remote_dirs_expected and ([$accepted.units_pending,$accepted.units_running,$accepted.units_failed,$accepted.remote_dirs_pending,$accepted.remote_dirs_running,$accepted.remote_dirs_failed,$accepted.attempts_exhausted,$accepted.auth_failures,$accepted.quota_failures,$accepted.error_failures] | all(. == 0))),
      destination_verified:($dest.status == "verified" and numeric_fields($dest_counts;["directories_expected","directories_complete","directories_pending","directories_running","directories_failed","directories_mismatched","files_expected","files_verified","bytes_expected","bytes_verified","mismatches","attempts_exhausted","auth_failures","quota_failures","error_failures"]) and (["missing","unexpected","type","size","sha1","mtime","duplicate","unreadable-name"] | all(($dest_counts.mismatch_classes[.] | type) == "number" and $dest_counts.mismatch_classes[.] == 0)) and $dest_counts.directories_complete == $dest_counts.directories_expected and $dest_counts.files_verified == $dest_counts.files_expected and $dest_counts.bytes_verified == $dest_counts.bytes_expected and ([$dest_counts.directories_pending,$dest_counts.directories_running,$dest_counts.directories_failed,$dest_counts.directories_mismatched,$dest_counts.mismatches,$dest_counts.attempts_exhausted,$dest_counts.auth_failures,$dest_counts.quota_failures,$dest_counts.error_failures] | all(. == 0))),
      snapshot_bindings_match:([$ids,$digests,$algorithms,$generations,$frozen_times] | all((. | all(. != null)) and ((. | unique | length) == 1))),
      phase_binding_tuples_match:($bindings | all(. != null and .snapshot_id == $ids[0] and .snapshot_digest == $digests[0] and .snapshot_digest_algorithm == $algorithms[0] and .snapshot_generation == $generations[0] and .frozen_at == $frozen_times[0] and .source_account_fingerprint == $source_fps[0] and .destination_account_fingerprint == $destination_fps[0] and .versions == $versions[0])),
      source_account_ready:($source_acct.kind == "pcloud_account" and $source_acct.status == "verified" and ($source_acct.generated_at | type) == "string" and ($source_acct.source_account_fingerprint | type) == "string" and ($source_acct.quota | has("quota_bytes")) and ($source_acct.quota | has("used_bytes"))),
      destination_account_ready:($acct.kind == "proton_account" and $acct.status == "verified" and ($acct.generated_at | type) == "string" and ($acct.destination_account_fingerprint | type) == "string" and ($acct.quota | has("quota_bytes")) and ($acct.quota | has("used_bytes")) and (((($acct.quota.quota_bytes | type) == "number") and (($acct.quota.used_bytes | type) == "number") and ($acct.quota.quota_bytes - $acct.quota.used_bytes) >= $source_counts.bytes_indexed) or ($capacity_ack == true and $acct.quota.quota_bytes == null and $acct.quota.used_bytes == null))),
      account_bindings_match:(($source_fps | all(. != null)) and (($source_fps | unique | length) == 1) and ($destination_fps | all(. != null)) and (($destination_fps | unique | length) == 1)),
      no_error_auth_quota_or_exhaustion:(numeric_fields($up;["attempts_exhausted","auth_failures","quota_failures","error_failures"]) and numeric_fields($dest;["attempts_exhausted","auth_failures","quota_failures","error_failures"]) and ([$up.attempts_exhausted,$up.auth_failures,$up.quota_failures,$up.error_failures,$dest.attempts_exhausted,$dest.auth_failures,$dest.quota_failures,$dest.error_failures] | all(. == 0))),
      versions_recorded:($versions[0] != null and ([$versions[0].toolkit,$versions[0].bundle_sha256,$versions[0].python,$versions[0].rclone,$versions[0].proton_version,$versions[0].expected_proton_version] | all(type == "string" and length > 0)) and $versions[0].version_compatible == true),
      compatibility_proven:($compatibility.status == "compatible" and $compatibility.version_compatible == true and $compatibility.filesystem_interface_compatible == true and $compatibility.proton_version == $versions[0].expected_proton_version),
      controller_phases_complete:([$inventory[0],$checksums[0],$freeze[0],$download[0],$metadata[0],$reconcile[0],$local_verify[0],$freshness[0],$upload_phase[0],$destination_phase[0]] | all(.state == "complete" and .exit_code == 0 and .running == false)),
      attention_absent:($attention[0] == null)}) as $predicates |
    {kind:"pcloud_proton_migration",observed_at:$at,run_id:$run_id,versions:$versions[0],compatibility_result:$compatibility,
     source:{remote:$source_remote,root:$source_root,inventory:$source_inventory[0],snapshot:$snapshot},staging:$staging,
     source_account:$source_acct,destination_account:$acct,source_freshness:$source_freshness[0],local_verification:$local,upload_acceptance:$accepted,
     phases:{inventory:$inventory[0],checksums:$checksums[0],source_freeze:$freeze[0],download:$download[0],metadata_apply:$metadata[0],reconcile:$reconcile[0],local_verify:$local_verify[0],source_freshness:$freshness[0],remediation:$remediate[0],upload:$upload_phase[0],destination_verify:$destination_phase[0]},
     upload:$up,destination_verification:$dest,attention_required:$attention[0],events:$events,
     completion_gate:{satisfied:([$predicates[]] | all(. == true)),predicates:$predicates,snapshot:{snapshot_id:($ids[0] // null),snapshot_digest:($digests[0] // null),snapshot_digest_algorithm:($algorithms[0] // null),snapshot_generation:($generations[0] // null),frozen_at:($frozen_times[0] // null)},source_account_fingerprint:($source_fps[0] // null),destination_account_fingerprint:($destination_fps[0] // null)}}'
  trap - RETURN
  rm -rf "$temporary"
}

status_human() {
  overall_json | jq -r '
    "timestamp=\(.observed_at)\nrun_id=\(.run_id)\nsource=\(.source.remote)\(.source.root)\nstaging=\(.staging)",
    (.phases | to_entries[] | "\(.key): state=\(.value.state) running=\(.value.running) updated_at=\(.value.updated_at // "none")"),
    "upload_acceptance: status=\(.upload_acceptance.status // "not_started") accepted=\(.upload_acceptance.files_accepted // 0)/\(.upload_acceptance.files_expected // 0)",
    "destination_verification: status=\(.destination_verification.status // "unknown") files=\(.destination_verification.files_verified // 0)/\(.destination_verification.files_expected // 0)",
    "attention_required=\(if .attention_required == null then "no" else (.attention_required.phase + ": " + .attention_required.reason) end)",
    "events=\(.events)"'
}

phase_state() { pcm_phase_status_json "$1" | jq -r '.state'; }

archive_attention() {
  local phase="$1" attention="$PCM_BASE_DIR/ATTENTION_REQUIRED.json" archive_dir recorded
  [ -f "$attention" ] || return 0
  recorded="$(jq -r '.phase // empty' "$attention")"
  case "$phase:$recorded" in
    proton-verify:destination-verification|proton-verify:proton-verify|"$recorded:$recorded") ;;
    *) return 0 ;;
  esac
  archive_dir="$PCM_BASE_DIR/attention-history"
  mkdir -p "$archive_dir"
  mv "$attention" "$archive_dir/$(date -u +%Y%m%dT%H%M%SZ)-$phase.json"
  pcm_event "$phase" attention_cleared 'recovery condition verified'
}

supervisor_preflight() {
  local log="$PCM_BASE_DIR/preflight/latest.log" recovery versions
  recovery="$PUBLIC_CLI config validate; $PUBLIC_CLI doctor; $PUBLIC_CLI supervisor-run"
  mkdir -p "$(dirname "$log")"
  if ! source_account_refresh || ! destination_account_refresh; then
    printf '%s\n' 'account identity or Proton compatibility gate failed' >"$log"
    pcm_write_attention supervisor-preflight 'account identity or Proton compatibility gate failed' "$PUBLIC_CLI source account status; $PUBLIC_CLI compatibility probe; $PUBLIC_CLI supervisor-run" account_or_compatibility
    return 2
  fi
  if ! versions="$(persist_tool_versions)"; then
    printf '%s\n' 'static toolkit/dependency version collection failed' >"$log"
    pcm_write_attention supervisor-preflight 'static toolkit/dependency version collection failed' "$recovery" version_evidence
    return 2
  fi
  if "$SCRIPT_DIR/vps-preflight.sh" --offline --quiet --config "$PCM_CONFIG_FILE" >"$log" 2>&1; then
    archive_attention supervisor-preflight
    pcm_event supervisor-preflight complete 'offline preflight passed'
    return 0
  fi
  pcm_write_attention supervisor-preflight 'offline preflight failed; inspect the durable preflight log' "$recovery" preflight_failure
  return 2
}

start_selected_phase() {
  case "$1" in
    inventory) inventory_start ;;
    checksums) checksums_start ;;
    download) download_start ;;
    metadata-apply) metadata_apply_start ;;
    reconcile) reconcile_start ;;
    verify-local) verify_local_start sha1 ;;
    source-freshness) remote_check_start ;;
    upload) upload_start ;;
    proton-verify) proton_verify_start ;;
  esac
}

supervise() {
  local phase state upload_state verify_state code
  prepare_state
  mkdir -p "$PCM_BASE_DIR/locks"
  exec 5>"$PCM_BASE_DIR/locks/supervisor.lock"
  if ! flock -n 5; then
    pcm_event supervisor lock_contended 'another supervisor invocation owns the global lock' lock_contended
    printf '{"kind":"supervisor","status":"already_running"}\n'
    return 0
  fi
  supervisor_preflight || return
  if [ -f "$PCM_BASE_DIR/ATTENTION_REQUIRED.json" ]; then
    cat "$PCM_BASE_DIR/ATTENTION_REQUIRED.json"
    return 2
  fi

  for phase in inventory checksums; do
    state="$(phase_state "$phase")"
    case "$state" in
      complete) continue ;;
      not_started|interrupted) start_selected_phase "$phase"; return ;;
      starting|running) pcm_phase_status_json "$phase"; return ;;
      failed|blocked)
        pcm_write_attention "$phase" "$phase requires classified recovery" "$(phase_recovery "$phase")" phase_failure
        return 2
        ;;
      *) pcm_write_attention "$phase" "unknown phase state: $state" "$(phase_recovery "$phase")" state_error; return 2 ;;
    esac
  done

  if [ ! -f "$FROZEN_MANIFEST" ] || [ "$(phase_state source-freeze)" != complete ]; then
    source_freeze
    return
  fi
  if ! verify_frozen_manifest; then
    pcm_write_attention source-freeze 'frozen snapshot/account binding verification failed' "$(phase_recovery source-freeze)" binding_failure
    return 2
  fi

  for phase in download metadata-apply reconcile verify-local source-freshness; do
    state="$(phase_state "$phase")"
    case "$state" in
      complete) continue ;;
      not_started|interrupted) start_selected_phase "$phase"; return ;;
      starting|running) pcm_phase_status_json "$phase"; return ;;
      failed|blocked)
        pcm_write_attention "$phase" "$phase requires classified recovery" "$(phase_recovery "$phase")" phase_failure
        return 2
        ;;
      *) pcm_write_attention "$phase" "unknown phase state: $state" "$(phase_recovery "$phase")" state_error; return 2 ;;
    esac
  done

  if [ ! -f "$PROTON_STATE/upload.sqlite" ]; then
    if upload_plan; then return; else
      code=$?
      pcm_write_attention upload-plan "upload planning/account/capacity gate failed with exit code $code" "$(phase_recovery upload-plan)" capacity_or_account
      return "$code"
    fi
  fi
  upload_state="$(upload_status | jq -r '.status // "unknown"')"
  case "$upload_state" in
    complete)
      if [ ! -f "$UPLOAD_ACCEPTED_EVIDENCE" ]; then
        if write_upload_acceptance; then return; else
          pcm_write_attention upload-acceptance 'upload completed but acceptance evidence did not satisfy all gates' "$(phase_recovery upload-acceptance)" acceptance_failure
          return 2
        fi
      fi
      ;;
    recoverable) upload_recover_start; return ;;
    planned|uploading|preparing-directories) upload_start; return ;;
    blocked-authentication) pcm_write_attention upload "$upload_state" "$PUBLIC_CLI auth login; $(phase_recovery upload)" authentication; return 2 ;;
    blocked-*|failed-*|unknown) pcm_write_attention upload "$upload_state" "$(phase_recovery upload)" destination_failure; return 2 ;;
    *) upload_start; return ;;
  esac

  verify_state="$(proton_verify_status | jq -r '.status // "unknown"')"
  case "$verify_state" in
    verified) completion_gate ;;
    not_started|running|recoverable) proton_verify_start ;;
    blocked-authentication) pcm_write_attention destination-verification "$verify_state" "$PUBLIC_CLI auth login; $(phase_recovery proton-verify)" authentication; return 2 ;;
    failed-*|blocked-*|unknown) pcm_write_attention destination-verification "$verify_state" "$(phase_recovery proton-verify)" verification_failure; return 2 ;;
    *) proton_verify_start ;;
  esac
}

write_completion_gate_failure() {
  local failure_class="$1" reason="$2" exit_code="$3"
  mkdir -p "$PCM_BASE_DIR/reports"
  jq -cn --arg at "$(pcm_now)" --arg failure_class "$failure_class" --arg reason "$reason" \
    --argjson exit_code "$exit_code" --arg frozen_manifest "$FROZEN_MANIFEST" \
    --arg local_audit "$PCM_BASE_DIR/reports/download-completion-audit.json" \
    --arg upload_acceptance "$UPLOAD_ACCEPTED_EVIDENCE" --arg upload_db "$PROTON_STATE/upload.sqlite" \
    --arg verification_db "$PROTON_VERIFY_STATE/verify.sqlite" --arg events "$(pcm_events_file)" \
    '{kind:"migration_completion_gate",checked_at:$at,status:"incomplete",verified:false,predicates:{preconditions_satisfied:false},failure:{class:$failure_class,reason:$reason,exit_code:$exit_code},attention_required:true,evidence:{frozen_manifest:$frozen_manifest,local_audit:$local_audit,upload_acceptance:$upload_acceptance,upload_db:$upload_db,verification_db:$verification_db,events:$events}}' |
    pcm_atomic_write "$PCM_BASE_DIR/reports/completion-gate-latest.json"
}

completion_gate() {
  local status gate phase_dir local_report source_freshness_report destination_report remediation_evidence code
  if verify_frozen_manifest; then :; else
    code=$?
    write_completion_gate_failure frozen_manifest 'frozen manifest precondition failed' "$code"
    pcm_write_attention completion 'completion gate frozen-manifest precondition failed' "$PUBLIC_CLI source freeze; $PUBLIC_CLI completion gate" completion_gate_failure
    cat "$PCM_BASE_DIR/reports/completion-gate-latest.json"
    return "$code"
  fi
  if archive_attention completion; then :; else
    code=$?
    write_completion_gate_failure precondition_status 'completion attention precondition could not be evaluated' "$code"
    pcm_write_attention completion 'completion gate precondition status failed' "$PUBLIC_CLI completion gate" completion_gate_failure
    cat "$PCM_BASE_DIR/reports/completion-gate-latest.json"
    return "$code"
  fi
  if status="$(overall_json)"; then :; else
    code=$?
    write_completion_gate_failure status 'overall completion status could not be produced' "$code"
    pcm_write_attention completion 'completion gate status generation failed' "$PUBLIC_CLI completion gate" completion_gate_failure
    cat "$PCM_BASE_DIR/reports/completion-gate-latest.json"
    return "$code"
  fi
  if gate="$(jq --arg frozen_manifest "$FROZEN_MANIFEST" --arg local_audit "$PCM_BASE_DIR/reports/download-completion-audit.json" \
    --arg upload_acceptance "$UPLOAD_ACCEPTED_EVIDENCE" --arg upload_db "$PROTON_STATE/upload.sqlite" \
    --arg verification_db "$PROTON_VERIFY_STATE/verify.sqlite" --arg events "$(pcm_events_file)" \
    '{kind:"migration_completion_gate",checked_at:.observed_at,status:(if .completion_gate.satisfied then "complete" else "incomplete" end),verified:.completion_gate.satisfied,predicates:.completion_gate.predicates,snapshot:.completion_gate.snapshot,source_account_fingerprint:.completion_gate.source_account_fingerprint,destination_account_fingerprint:.completion_gate.destination_account_fingerprint,versions:.versions,compatibility_result:.compatibility_result,attention_required:(.attention_required != null),evidence:{frozen_manifest:$frozen_manifest,local_audit:$local_audit,upload_acceptance:$upload_acceptance,upload_db:$upload_db,verification_db:$verification_db,events:$events}}' <<<"$status")"; then :; else
    code=$?
    write_completion_gate_failure status 'overall completion status did not satisfy the completion report schema' "$code"
    pcm_write_attention completion 'completion gate status parsing failed' "$PUBLIC_CLI completion gate" completion_gate_failure
    cat "$PCM_BASE_DIR/reports/completion-gate-latest.json"
    return "$code"
  fi
  mkdir -p "$PCM_BASE_DIR/reports"
  jq -c . <<<"$gate" | pcm_atomic_write "$PCM_BASE_DIR/reports/completion-gate-latest.json"
  if ! jq -e '.completion_gate.satisfied == true' <<<"$status" >/dev/null; then
    pcm_write_attention completion 'completion gate failed; final handoff was not written' "$PUBLIC_CLI completion gate" completion_gate_failure
    printf '%s\n' "$gate"
    return 1
  fi
  phase_dir="$(pcm_current_phase_dir verify-local)"; local_report="${phase_dir:+$phase_dir/report.json}"
  phase_dir="$(pcm_current_phase_dir source-freshness)"; source_freshness_report="${phase_dir:+$phase_dir/report.json}"
  phase_dir="$(pcm_current_phase_dir proton-verify)"; destination_report="${phase_dir:+$phase_dir/progress.json}"
  remediation_evidence="$PROTON_STATE/remediation-evidence.json"
  [ -f "$remediation_evidence" ] || remediation_evidence=""
  jq -cn --arg at "$(pcm_now)" --argjson status "$status" \
    --arg frozen_manifest "$FROZEN_MANIFEST" --arg local_audit "$PCM_BASE_DIR/reports/download-completion-audit.json" \
    --arg upload_acceptance "$UPLOAD_ACCEPTED_EVIDENCE" --arg upload_db "$PROTON_STATE/upload.sqlite" \
    --arg verification_db "$PROTON_VERIFY_STATE/verify.sqlite" --arg events "$(pcm_events_file)" \
    --arg local_report "$local_report" --arg source_freshness_report "$source_freshness_report" \
    --arg destination_report "$destination_report" --arg remediation_evidence "$remediation_evidence" '
    ($status.source.inventory) as $source_counts |
    {kind:"pcloud_proton_migration_final_handoff",completed_at_utc:$at,run_id:$status.run_id,
     versions:$status.versions,compatibility_result:$status.compatibility_result,
     snapshot:{id:$status.completion_gate.snapshot.snapshot_id,digest:$status.completion_gate.snapshot.snapshot_digest,digest_algorithm:$status.completion_gate.snapshot.snapshot_digest_algorithm,generation:$status.completion_gate.snapshot.snapshot_generation,frozen_at:$status.completion_gate.snapshot.frozen_at,source_account_fingerprint:$status.completion_gate.source_account_fingerprint,destination_account_fingerprint:$status.completion_gate.destination_account_fingerprint},
     source:{files:$source_counts.files_indexed,directories:$source_counts.directory_entries_indexed,bytes:$source_counts.bytes_indexed,sha1_expected:$source_counts.files_indexed,sha1_complete:$source_counts.files_with_sha1,ambiguous_entries:$source_counts.ambiguous_entries,unreadable_names:$source_counts.unreadable_names},
     local:{files_expected:$status.local_verification.files_expected,files_verified:$status.local_verification.files_verified,directories_expected:$status.local_verification.directories_expected,directories_verified:$status.local_verification.directories_verified,bytes_expected:$status.local_verification.bytes_expected,bytes_verified:$status.local_verification.bytes_verified,sha1_expected:$status.local_verification.sha1_expected,sha1_verified:$status.local_verification.sha1_verified,mismatches:$status.local_verification.mismatch_classes},
     upload:{units_expected:$status.upload_acceptance.units_expected,units_accepted:$status.upload_acceptance.units_complete,files_expected:$status.upload_acceptance.files_expected,files_accepted:$status.upload_acceptance.files_complete,bytes_expected:$status.upload_acceptance.bytes_expected,bytes_accepted:$status.upload_acceptance.bytes_complete,directories_expected:$status.upload_acceptance.remote_dirs_expected,directories_accepted:$status.upload_acceptance.remote_dirs_complete,failed_units:$status.upload_acceptance.units_failed,failed_directories:$status.upload_acceptance.remote_dirs_failed,attempts_exhausted:$status.upload_acceptance.attempts_exhausted},
     destination:{files_expected:$status.destination_verification.files_expected,files_verified:$status.destination_verification.files_verified,directories_expected:$status.destination_verification.directories_expected,directories_complete:$status.destination_verification.directories_complete,bytes_expected:$status.destination_verification.bytes_expected,bytes_verified:$status.destination_verification.bytes_verified,mismatches:{missing:$status.destination_verification.mismatch_classes.missing,unexpected:$status.destination_verification.mismatch_classes.unexpected,type:$status.destination_verification.mismatch_classes.type,size:$status.destination_verification.mismatch_classes.size,sha1:$status.destination_verification.mismatch_classes.sha1,mtime:$status.destination_verification.mismatch_classes.mtime},blockers:{duplicate:$status.destination_verification.mismatch_classes.duplicate,unreadable_name:$status.destination_verification.mismatch_classes["unreadable-name"]}},
     remediation:{count:$status.upload.remediation_count,evidence_path:(if $remediation_evidence == "" then null else $remediation_evidence end)},
     quota:{used_bytes:$status.destination_account.quota.used_bytes,quota_bytes:$status.destination_account.quota.quota_bytes,available_bytes:(if (($status.destination_account.quota.used_bytes | type) == "number" and ($status.destination_account.quota.quota_bytes | type) == "number") then ($status.destination_account.quota.quota_bytes - $status.destination_account.quota.used_bytes) else null end)},
     controller_results:($status.phases | with_entries(.value = {state:.value.state,exit_code:.value.exit_code,running:.value.running,updated_at:.value.updated_at})),
     terminal_status:{attention_absent:($status.attention_required == null),remediation_blocked:($status.upload.status == "blocked-remediation"),exhaustion:($status.upload.attempts_exhausted > 0 or $status.destination_verification.attempts_exhausted > 0),authentication_blocked:($status.upload.auth_failures > 0 or $status.destination_verification.auth_failures > 0),quota_blocked:($status.upload.quota_failures > 0 or $status.destination_verification.quota_failures > 0),error_state:($status.upload.error_failures > 0 or $status.destination_verification.error_failures > 0)},
     completion_gate:{satisfied:$status.completion_gate.satisfied,predicates:$status.completion_gate.predicates},
     evidence:{frozen_manifest:$frozen_manifest,local_audit:$local_audit,local_verification_report:$local_report,source_freshness_report:$source_freshness_report,upload_acceptance:$upload_acceptance,upload_db:$upload_db,remediation_evidence:(if $remediation_evidence == "" then null else $remediation_evidence end),verification_db:$verification_db,destination_verification_report:$destination_report,events:$events},
     limitations:["Original pCloud creation time cannot be set through the Proton CLI.","Proton SHA1 is upload-client-claimed revision metadata, not an independent server-side plaintext digest."]}' |
    pcm_atomic_write "$FINAL_HANDOFF"
  pcm_event completion verified "final_handoff=$FINAL_HANDOFF"
  cat "$FINAL_HANDOFF"
}

recover() {
  cat <<EOF
source inventory resume:  $PUBLIC_CLI source inventory resume
source checksums resume:  $PUBLIC_CLI source checksums resume
source freeze:            $PUBLIC_CLI source freeze
download resume:          $PUBLIC_CLI download resume
metadata resume:          $PUBLIC_CLI local metadata resume
reconcile resume:         $PUBLIC_CLI local reconcile resume
local verify resume:      $PUBLIC_CLI local verify resume
freshness resume:         $PUBLIC_CLI source freshness resume
local remediation resume: $PUBLIC_CLI local remediate resume
upload resume:            $PUBLIC_CLI upload resume
destination verify resume:$PUBLIC_CLI destination verify resume
EOF
}

case "${1:-}" in
  __run-phase) shift; pcm_run_phase "$@" ;;
  inventory-start) inventory_start ;;
  inventory-status) inventory_status ;;
  checksums-start) checksums_start ;;
  source-freeze) source_freeze ;;
  download-start) download_start ;;
  metadata-apply-start) metadata_apply_start ;;
  reconcile-start) reconcile_start ;;
  verify-local-start) shift; verify_local_start "${1:-sha1}" ;;
  remote-check-start) remote_check_start ;;
  remediate-start) remediate_start ;;
  phase-status) [ "$#" -eq 2 ] || { usage >&2; exit 2; }; pcm_phase_status_json "$2" ;;
  status) if [ "${2:-}" = --json ]; then overall_json; else status_human; fi ;;
  recover) recover ;;
  audit) audit ;;
  account-status) account_status ;;
  source-account-status) source_account_status ;;
  compatibility-status) compatibility_status ;;
  upload-plan) upload_plan ;;
  upload-start) upload_start ;;
  upload-recover-start) upload_recover_start ;;
  upload-status) upload_status ;;
  proton-verify-start) proton_verify_start ;;
  proton-verify-status) proton_verify_status ;;
  supervise|autonomous-start) supervise ;;
  completion-gate) completion_gate ;;
  *) usage >&2; exit 2 ;;
esac
