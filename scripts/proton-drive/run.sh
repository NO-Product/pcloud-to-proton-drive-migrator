#!/usr/bin/env bash
set -Eeuo pipefail

# This wrapper never accepts, reads, or synthesizes credentials. Its exact
# interface is: run.sh PROTON_BINARY COMMAND... .
readonly AUTH_REQUIRED_EXIT=78
if [ "$#" -lt 2 ]; then
  printf 'usage: %s PROTON_BINARY COMMAND...\n' "$0" >&2
  exit 64
fi
proton_bin=$1
shift
[ -x "$proton_bin" ] || { printf 'Proton binary is not executable: %s\n' "$proton_bin" >&2; exit 69; }
config_home=${XDG_CONFIG_HOME:-$HOME/.config}
data_home=${XDG_DATA_HOME:-$HOME/.local/share}
cache_home=${XDG_CACHE_HOME:-$HOME/.cache}
for directory in "$config_home" "$data_home" "$cache_home"; do
  case "$directory" in /*) ;; *) printf 'XDG runtime directories must be absolute\n' >&2; exit 64;; esac
  install -d -m 0700 "$directory"
  [ -O "$directory" ] || { printf 'runtime directory is not owned by uid %s: %s\n' "$(id -u)" "$directory" >&2; exit 73; }
done

if [ "${1:-}" = account ] && { [ "${2:-}" = login ] || [ "${2:-}" = info ] || [ "${2:-}" = logout ]; }; then
  exec "$proton_bin" "$@"
fi
if ! "$proton_bin" account info >/dev/null 2>&1; then
  printf 'Proton authentication required; use the public auth login command\n' >&2
  exit "$AUTH_REQUIRED_EXIT"
fi
exec "$proton_bin" "$@"
