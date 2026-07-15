#!/usr/bin/env bash

pcm_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

pcm_atomic_write() {
  local target="$1" directory temporary
  directory="$(dirname "$target")"
  mkdir -p "$directory"
  temporary="$(mktemp "$directory/.pcm-write.XXXXXX")"
  trap 'rm -f "$temporary"' RETURN
  cat > "$temporary"
  python3 -c 'import os,sys; fd=os.open(sys.argv[1], os.O_RDONLY); os.fsync(fd); os.close(fd)' "$temporary"
  mv -f "$temporary" "$target"
  python3 -c 'import os,sys; fd=os.open(sys.argv[1], os.O_RDONLY); os.fsync(fd); os.close(fd)' "$directory"
  trap - RETURN
}

pcm_ensure_run_id() {
  local file="$PCM_BASE_DIR/run-id" candidate lock
  mkdir -p "$PCM_BASE_DIR" "$PCM_BASE_DIR/locks"
  lock="$PCM_BASE_DIR/locks/run-id.lock"
  exec 8>"$lock"
  flock 8
  if [ ! -s "$file" ]; then
    if [ -r /proc/sys/kernel/random/uuid ]; then
      candidate="$(cat /proc/sys/kernel/random/uuid)"
    elif command -v uuidgen >/dev/null 2>&1; then
      candidate="$(uuidgen | tr '[:upper:]' '[:lower:]')"
    else
      candidate="$(date -u +%Y%m%dT%H%M%SZ)-$$-$RANDOM"
    fi
    printf '%s\n' "$candidate" | pcm_atomic_write "$file"
  fi
  PCM_RUN_ID="$(cat "$file")"
  export PCM_RUN_ID
}

pcm_existing_run_id() {
  if [ -s "$PCM_BASE_DIR/run-id" ]; then cat "$PCM_BASE_DIR/run-id"; else printf 'not-created'; fi
}

pcm_events_file() { printf '%s/events/events.jsonl' "$PCM_BASE_DIR"; }

pcm_error_domain() {
  case "$1" in
    inventory|checksums|source-freeze|source-freshness|download|remediate) printf source ;;
    metadata-apply|reconcile|verify-local) printf local ;;
    upload|proton-verify|destination-verification) printf destination ;;
    *) printf shell ;;
  esac
}

pcm_event() {
  local phase="$1" event="$2" message="${3:-}" error_class="${4:-}" file lock line domain
  file="$(pcm_events_file)"
  lock="$PCM_BASE_DIR/locks/events.lock"
  mkdir -p "$(dirname "$file")" "$PCM_BASE_DIR/locks"
  domain="$(pcm_error_domain "$phase")"
  line="$(jq -cn --arg at "$(pcm_now)" --arg run_id "${PCM_RUN_ID:-$(pcm_existing_run_id)}" --arg phase "$phase" --arg event "$event" --arg message "$message" --arg error_domain "$domain" --arg error_class "$error_class" '{at:$at,run_id:$run_id,phase:$phase,event:$event,message:$message,error_domain:$error_domain,error_class:(if $error_class == "" then null else $error_class end)}')"
  exec 7>"$lock"
  flock 7
  printf '%s\n' "$line" >> "$file"
}

pcm_operation_root() { printf '%s/operations' "$PCM_BASE_DIR"; }
pcm_phase_dir() { printf '%s/%s/%s' "$(pcm_operation_root)" "$1" "${PCM_RUN_ID:-$(pcm_existing_run_id)}"; }
pcm_current_phase_dir() { readlink -f "$(pcm_operation_root)/$1/current" 2>/dev/null || true; }

pcm_write_phase_status() {
  local phase="$1" state="$2" recovery="$3" code="${4:-0}" dir
  dir="$(pcm_phase_dir "$phase")"
  mkdir -p "$dir"
  jq -cn --arg phase "$phase" --arg state "$state" --arg run_id "$PCM_RUN_ID" --arg updated_at "$(pcm_now)" --arg recovery "$recovery" --argjson exit_code "$code" \
    '{phase:$phase,state:$state,run_id:$run_id,updated_at:$updated_at,recovery:$recovery,exit_code:$exit_code}' | pcm_atomic_write "$dir/status.json"
  printf '%s\n' "$state" | pcm_atomic_write "$dir/status"
}

pcm_file_timestamp() {
  local file="$1"
  [ -e "$file" ] || { printf ''; return; }
  stat -c '%y' "$file" 2>/dev/null || stat -f '%Sm' -t '%Y-%m-%dT%H:%M:%SZ' "$file" 2>/dev/null || true
}

pcm_boot_id() { cat /proc/sys/kernel/random/boot_id 2>/dev/null || printf unknown; }

pcm_proc_start_time() {
  local line rest
  line="$(cat "/proc/$1/stat" 2>/dev/null)" || return 1
  rest="${line##*) }"
  awk '{print $20}' <<<"$rest"
}

pcm_process_identity_json() {
  local pid="$1" start executable
  start="$(pcm_proc_start_time "$pid")" || return 1
  executable="$(readlink -f "/proc/$pid/exe" 2>/dev/null)" || return 1
  jq -cn --argjson pid "$pid" --arg boot_id "$(pcm_boot_id)" --arg proc_start_time "$start" --arg executable "$executable" \
    '{pid:$pid,boot_id:$boot_id,proc_start_time:$proc_start_time,executable:$executable}'
}

pcm_process_identity_alive() {
  local file="$1" pid boot start executable
  [ -f "$file" ] || return 1
  pid="$(jq -r '.pid // empty' "$file")"
  boot="$(jq -r '.boot_id // empty' "$file")"
  start="$(jq -r '.proc_start_time // empty' "$file")"
  executable="$(jq -r '.executable // empty' "$file")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 1
  [ "$boot" = "$(pcm_boot_id)" ] || return 1
  [ "$start" = "$(pcm_proc_start_time "$pid" 2>/dev/null)" ] || return 1
  [ "$executable" = "$(readlink -f "/proc/$pid/exe" 2>/dev/null)" ] || return 1
}

pcm_phase_status_json() {
  local phase="$1" dir pid progress_at state_file running identity identity_json=null binding_json=null
  dir="$(pcm_current_phase_dir "$phase")"
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    jq -cn --arg phase "$phase" '{phase:$phase,state:"not_started",run_id:null,updated_at:null,progress_updated_at:null,pid:null,running:false,recovery:null}'
    return
  fi
  identity="$dir/process.json"
  pid="$(jq -r '.pid // empty' "$identity" 2>/dev/null || true)"
  if [ -f "$identity" ]; then identity_json="$(jq -c . "$identity" 2>/dev/null || printf null)"; fi
  if [ -f "$dir/binding.json" ]; then binding_json="$(jq -c . "$dir/binding.json" 2>/dev/null || printf null)"; fi
  progress_at="$(pcm_file_timestamp "$dir/progress.json")"
  if pcm_process_identity_alive "$identity"; then running=true; else running=false; fi
  state_file="$dir/status.json"
  if [ -f "$state_file" ]; then
    jq --arg progress_at "$progress_at" --arg pid "$pid" --argjson running "$running" --argjson identity "$identity_json" --argjson binding "$binding_json" \
      '. + {state:(if ((.state == "running" or .state == "starting") and ($running|not)) then "interrupted" else .state end),progress_updated_at:(if $progress_at == "" then null else $progress_at end),pid:(if $pid == "" then null else ($pid|tonumber? // $pid) end),process_identity:$identity,snapshot_binding:$binding,running:$running}' "$state_file"
  else
    jq -cn --arg phase "$phase" --arg state "$(cat "$dir/status" 2>/dev/null || printf unknown)" --arg progress_at "$progress_at" --arg pid "$pid" \
      --argjson running "$running" \
      --argjson binding "$binding_json" '{phase:$phase,state:(if (($state == "running" or $state == "starting") and ($running|not)) then "interrupted" else $state end),run_id:null,updated_at:null,progress_updated_at:(if $progress_at == "" then null else $progress_at end),pid:(if $pid == "" then null else ($pid|tonumber? // $pid) end),process_identity:null,snapshot_binding:$binding,running:$running,recovery:null}'
  fi
}

pcm_start_phase() {
  local phase="$1" recovery="$2" dir pid attempts launch_lock
  shift 2
  pcm_ensure_run_id
  dir="$(pcm_phase_dir "$phase")"
  mkdir -p "$dir" "$(pcm_operation_root)/$phase" "$PCM_BASE_DIR/locks"
  launch_lock="$PCM_BASE_DIR/locks/$phase.launch.lock"
  exec 6>"$launch_lock"
  flock 6
  if pcm_process_identity_alive "$dir/process.json"; then
    pcm_phase_status_json "$phase"
    return 0
  fi
  attempts="$(cat "$dir/attempts" 2>/dev/null || printf 0)"
  attempts=$((attempts + 1))
  printf '%s\n' "$attempts" | pcm_atomic_write "$dir/attempts"
  if [ "$attempts" -gt "$PCM_MAX_PHASE_ATTEMPTS" ]; then
    pcm_write_phase_status "$phase" blocked "$recovery" 75
    pcm_write_attention "$phase" "bounded launch attempts exhausted ($PCM_MAX_PHASE_ATTEMPTS)" "$recovery" exhaustion
    return 75
  fi
  ln -sfn "$dir" "$(pcm_operation_root)/$phase/current"
  pcm_write_phase_status "$phase" starting "$recovery"
  pcm_event "$phase" starting "$recovery"
  flock -u 6
  exec 6>&-
  pid="${BASHPID:-$$}"
  pcm_process_identity_json "$pid" | pcm_atomic_write "$dir/process.json"
  printf '%s\n' "$pid" | pcm_atomic_write "$dir/pid"
  "$PCM_FULL_SYNC" __run-phase "$phase" "$recovery" "$@" </dev/null >>"$dir/stdout.log" 2>>"$dir/stderr.log"
  pcm_phase_status_json "$phase"
}

pcm_run_phase() {
  local phase="$1" recovery="$2" code=0 lock
  shift 2
  pcm_ensure_run_id
  lock="$PCM_BASE_DIR/locks/$phase.lock"
  exec 9>"$lock"
  if ! flock -n 9; then
    pcm_event "$phase" lock_contended 'another process owns the phase lock'
    return 75
  fi
  pcm_write_phase_status "$phase" running "$recovery"
  pcm_event "$phase" running ''
  if "$@"; then
    pcm_write_phase_status "$phase" complete "$recovery" 0
    pcm_event "$phase" complete ''
    if declare -F archive_attention >/dev/null 2>&1; then archive_attention "$phase"; fi
  else
    code=$?
    pcm_write_phase_status "$phase" failed "$recovery" "$code"
    pcm_event "$phase" failed "exit_code=$code" phase_failure
    pcm_write_attention "$phase" "phase failed with exit code $code" "$recovery" phase_failure
    return "$code"
  fi
}

pcm_write_attention() {
  local phase="$1" reason="$2" action="$3" error_class="${4:-attention_required}" domain
  domain="$(pcm_error_domain "$phase")"
  jq -cn --arg at "$(pcm_now)" --arg phase "$phase" --arg reason "$reason" --arg required_action "$action" --arg error_domain "$domain" --arg error_class "$error_class" \
    '{at:$at,phase:$phase,reason:$reason,required_action:$required_action,error_domain:$error_domain,error_class:$error_class}' | pcm_atomic_write "$PCM_BASE_DIR/ATTENTION_REQUIRED.json"
  pcm_event "$phase" attention_required "$reason" "$error_class"
}
