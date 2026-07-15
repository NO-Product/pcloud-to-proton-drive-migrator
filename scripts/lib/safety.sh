#!/usr/bin/env bash

pcm_is_source_path() {
  [[ "$1" == "${PCM_SOURCE_REMOTE%:}:"* ]]
}

pcm_assert_not_source_destination() {
  local destination="$1"
  if pcm_is_source_path "$destination"; then
    printf 'safety refusal: configured pCloud source can never be a destination: %s\n' "$destination" >&2
    return 64
  fi
}

pcm_guard_rclone() {
  local command="${1:-}" source="${2:-}" destination="${3:-}"
  case "$command" in
    sync|bisync|delete|deletefile|purge|move|moveto|copyto|mkdir|rmdir|rmdirs|touch|cleanup|dedupe|mount|serve)
      printf 'safety refusal: rclone %s is not permitted by the source-read-only wrapper\n' "$command" >&2
      return 64
      ;;
    lsf|lsd|lsjson|size|sha1sum|md5sum)
      [ "$source" = "$PCM_SOURCE_SPEC" ] || { printf 'safety refusal: source read must use configured source %s\n' "$PCM_SOURCE_SPEC" >&2; return 64; }
      ;;
    copy|check)
      [ "$source" = "$PCM_SOURCE_SPEC" ] || { printf 'safety refusal: %s must read from configured source\n' "$command" >&2; return 64; }
      pcm_assert_not_source_destination "$destination"
      ;;
    *)
      printf 'safety refusal: rclone command is not allowlisted: %s\n' "${command:-missing}" >&2
      return 64
      ;;
  esac
}

pcm_rclone_source() {
  pcm_guard_rclone "$@"
  rclone --config "$PCM_RCLONE_CONFIG" "$@"
}
