#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG=/etc/pcloud-proton-migrate/migration.env
CONFIG_PROVIDED=0
DEFAULT_RUNTIME_USER=pcloud-proton
INSTALL_PACKAGES=1
while [ "$#" -gt 0 ]; do
  case "$1" in
    --config) CONFIG=${2:?missing path after --config}; CONFIG_PROVIDED=1; shift 2;;
    --no-packages) INSTALL_PACKAGES=0; shift;;
    -h|--help) printf 'usage: %s [--config PATH] [--no-packages]\n' "$0"; exit 0;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 64;;
  esac
done
[ "$(id -u)" -eq 0 ] || { printf 'run as root\n' >&2; exit 77; }
. /etc/os-release
case "${ID:-}" in debian|ubuntu) ;; *) printf 'Debian or Ubuntu is required\n' >&2; exit 69;; esac
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/config.sh"
source "$ROOT/scripts/lib/checks.sh"
readonly packages=(bash ca-certificates coreutils findutils jq python3 rclone rsync sed sqlite3 util-linux)
if [ "$INSTALL_PACKAGES" -eq 1 ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install --no-install-recommends --yes "${packages[@]}"
fi
if [ "$CONFIG_PROVIDED" -eq 0 ]; then
  PCM_RUNTIME_USER=$DEFAULT_RUNTIME_USER
else
  [ -f "$CONFIG" ] || { printf 'configured bootstrap requires the canonical PCM config: %s\n' "$CONFIG" >&2; exit 78; }
  unset PCM_CONFIG_LOADED
  MIGRATION_CONFIG=$CONFIG
  export MIGRATION_CONFIG
  pcm_load_config
fi
case "$PCM_RUNTIME_USER" in *[!A-Za-z0-9_-]*|'') printf 'invalid PCM_RUNTIME_USER\n' >&2; exit 65;; esac
getent group "$PCM_RUNTIME_USER" >/dev/null || groupadd --system "$PCM_RUNTIME_USER"
id "$PCM_RUNTIME_USER" >/dev/null 2>&1 || useradd --system --gid "$PCM_RUNTIME_USER" \
  --home-dir /var/lib/pcloud-proton-migrate --create-home --shell /usr/sbin/nologin "$PCM_RUNTIME_USER"
install -d -o root -g "$PCM_RUNTIME_USER" -m 0750 /etc/pcloud-proton-migrate
install -d -o "$PCM_RUNTIME_USER" -g "$PCM_RUNTIME_USER" -m 0700 \
  /var/lib/pcloud-proton-migrate /var/cache/pcloud-proton-migrate
if [ "$CONFIG_PROVIDED" -eq 0 ]; then
  printf 'Package/user bootstrap complete for PCM_RUNTIME_USER=%s. No storage was provisioned and no service was installed, enabled, or started.\n' "$PCM_RUNTIME_USER"
  exit 0
fi
pcm_provision_storage_paths
pcm_ensure_account_fingerprint_key
chown root:"$PCM_RUNTIME_USER" "$CONFIG"
chmod 0640 "$CONFIG"
printf 'Bootstrap complete for PCM_RUNTIME_USER=%s. No service was installed, enabled, or started.\n' "$PCM_RUNTIME_USER"
