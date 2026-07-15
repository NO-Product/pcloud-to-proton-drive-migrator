#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
INSTALL_ROOT=/opt/pcloud-proton-migrate
CONFIG_DIR=/etc/pcloud-proton-migrate
VERSION=
ENABLE=0
START=0
REPAIR=0
usage() {
  cat <<'HELP'
usage: install-autonomous-runner.sh --version VERSION [OPTIONS]
  --source DIR       source checkout
  --install-root DIR versioned installation root
  --config-dir DIR   preserved configuration directory
  --enable           enable timers; never implied
  --start            start timers; never implied
  --repair           explicitly replace an existing version with rollback
HELP
}
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version) VERSION=${2:?missing version}; shift 2;;
    --source) SOURCE_ROOT=${2:?missing source}; shift 2;;
    --install-root) INSTALL_ROOT=${2:?missing install root}; shift 2;;
    --config-dir) CONFIG_DIR=${2:?missing config dir}; shift 2;;
    --enable) ENABLE=1; shift;;
    --start) START=1; shift;;
    --repair) REPAIR=1; shift;;
    -h|--help) usage; exit 0;;
    *) printf 'unknown argument: %s\n' "$1" >&2; exit 64;;
  esac
done
[ "$(id -u)" -eq 0 ] || { printf 'run as root\n' >&2; exit 77; }
[ -n "$VERSION" ] || { printf '%s\n' '--version is required' >&2; exit 64; }
case "$VERSION" in *[!A-Za-z0-9._+-]*|.|..) printf 'invalid version: %s\n' "$VERSION" >&2; exit 64;; esac
SOURCE_ROOT=$(CDPATH= cd -- "$SOURCE_ROOT" 2>/dev/null && pwd) || { printf 'source directory does not exist\n' >&2; exit 66; }
case "$INSTALL_ROOT" in /*) ;; *) printf 'install root must be absolute\n' >&2; exit 64;; esac
case "$CONFIG_DIR" in /*) ;; *) printf 'config directory must be absolute\n' >&2; exit 64;; esac
case "$CONFIG_DIR" in *[[:space:]]*) printf 'config directory cannot contain whitespace\n' >&2; exit 64;; esac
for name in cp dd dirname find grep head install ln mktemp mv readlink rm sed sha256sum stat systemctl xargs; do
  command -v "$name" >/dev/null || { printf 'missing installer tool: %s\n' "$name" >&2; exit 69; }
done
readonly required_paths=(
  bin/pcloud-proton-migrate config/migration.env.example config/migration.env.schema
  deploy/systemd/pcloud-migration-supervisor.service deploy/systemd/pcloud-migration-supervisor.timer
  deploy/systemd/proton-progress-monitor.service deploy/systemd/proton-progress-monitor.timer
  scripts/lib/config.sh scripts/lib/checks.sh scripts/lib/runtime.sh scripts/lib/safety.sh
  scripts/migration scripts/migration/full-sync.sh scripts/migration/full-sync-status.sh
  scripts/migration/autonomous-source-pipeline.sh scripts/migration/supervise-proton-upload.sh
  scripts/migration/proton-progress-snapshot.sh scripts/migration/vps-preflight.sh
  scripts/migration/migration_common.py scripts/migration/pcloud-manifest.py
  scripts/migration/proton-account.py scripts/migration/proton-upload.py scripts/migration/proton-remediate.py
  scripts/migration/proton-verify.py scripts/migration/run-pcloud-manifest-account-bound.sh
  scripts/migration/run-proton-account-bound.sh
  scripts/proton-drive/run.sh pyproject.toml LICENSE NOTICE
)
for relative in "${required_paths[@]}"; do
  [ -e "$SOURCE_ROOT/$relative" ] || { printf 'incomplete source bundle; missing %s\n' "$relative" >&2; exit 66; }
done
PROJECT_VERSION=$(sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$SOURCE_ROOT/pyproject.toml" | head -n 1)
[ "$VERSION" = "$PROJECT_VERSION" ] || { printf 'requested version %s does not match pyproject version %s\n' "$VERSION" "${PROJECT_VERSION:-missing}" >&2; exit 65; }

install -d -o root -g root -m 0755 "$CONFIG_DIR"
if [ ! -e "$CONFIG_DIR/migration.env" ]; then
  install -o root -g root -m 0600 "$SOURCE_ROOT/config/migration.env.example" "$CONFIG_DIR/migration.env"
  printf 'installed config template; configure canonical PCM_* values and rerun: %s/migration.env\n' "$CONFIG_DIR" >&2
  exit 78
fi
# shellcheck disable=SC1091
source "$SOURCE_ROOT/scripts/lib/config.sh"
source "$SOURCE_ROOT/scripts/lib/checks.sh"
unset PCM_CONFIG_LOADED
MIGRATION_CONFIG="$CONFIG_DIR/migration.env"
export MIGRATION_CONFIG
pcm_load_config
case "$PCM_RUNTIME_USER" in *[!A-Za-z0-9_-]*|'') printf 'invalid PCM_RUNTIME_USER\n' >&2; exit 65;; esac
id "$PCM_RUNTIME_USER" >/dev/null 2>&1 || { printf 'missing PCM_RUNTIME_USER: %s\n' "$PCM_RUNTIME_USER" >&2; exit 69; }
getent group "$PCM_RUNTIME_USER" >/dev/null || { printf 'missing runtime group: %s\n' "$PCM_RUNTIME_USER" >&2; exit 69; }
chown root:"$PCM_RUNTIME_USER" "$CONFIG_DIR/migration.env"
chmod 0640 "$CONFIG_DIR/migration.env"
pcm_ensure_account_fingerprint_key

install -d -o root -g root -m 0755 "$INSTALL_ROOT" "$INSTALL_ROOT/releases"
release="$INSTALL_ROOT/releases/$VERSION"
if [ -e "$release" ] && [ "$REPAIR" -ne 1 ]; then
  printf 'release already exists; use explicit --repair: %s\n' "$release" >&2
  exit 73
fi
stage=$(mktemp -d "$INSTALL_ROOT/.release-$VERSION.XXXXXX")
bundle_manifest=$(mktemp "$INSTALL_ROOT/.bundle-$VERSION.XXXXXX")
cleanup() { rm -rf -- "$stage"; rm -f -- "$bundle_manifest"; }
trap cleanup EXIT
for relative in bin config deploy scripts pyproject.toml LICENSE NOTICE; do cp -a -- "$SOURCE_ROOT/$relative" "$stage/"; done
[ ! -d "$SOURCE_ROOT/src" ] || cp -a -- "$SOURCE_ROOT/src" "$stage/"
chown -R root:root "$stage"
find "$stage" -type d -exec chmod 0755 {} +
find "$stage" -type f -exec chmod go-w {} +
chmod 0755 "$stage/bin/pcloud-proton-migrate" "$stage/scripts/migration/"*.sh \
  "$stage/scripts/migration/"*.py "$stage/scripts/proton-drive/run.sh"
while IFS= read -r -d '' link; do
  resolved=$(readlink -f "$link")
  case "$resolved" in "$stage"/*) ;; *) printf 'bundle symlink escapes release: %s\n' "$link" >&2; exit 65;; esac
done < <(find "$stage" -type l -print0)
if find "$stage" \( -type f -o -type d \) -perm /022 -print -quit | grep -q .; then
  printf 'bundle contains group/world-writable content\n' >&2; exit 65
fi
(cd "$stage" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) > "$bundle_manifest"
install -o root -g root -m 0644 "$bundle_manifest" "$stage/BUNDLE-SHA256SUMS"
(cd "$stage" && sha256sum -c BUNDLE-SHA256SUMS >/dev/null)
printf 'copy-integrity self-check passed; this checksum manifest does not authenticate release provenance or publisher identity\n'
rm -f -- "$bundle_manifest"
previous_current=$(readlink "$INSTALL_ROOT/current" 2>/dev/null || true)
rollback_root=$(mktemp -d "$INSTALL_ROOT/.rollback-$VERSION.XXXXXX")
launcher=/usr/local/bin/pcloud-proton-migrate
if [ -e "$launcher" ] || [ -L "$launcher" ]; then
  cp -a -- "$launcher" "$rollback_root/launcher"
else
  : > "$rollback_root/launcher.absent"
fi
old_release=
switched=0
transaction_started=0
readonly units=(
  pcloud-migration-supervisor.service pcloud-migration-supervisor.timer
  proton-progress-monitor.service proton-progress-monitor.timer
)
declare -A timer_enabled_before=() timer_active_before=()
for unit in pcloud-migration-supervisor.timer proton-progress-monitor.timer; do
  timer_enabled_before["$unit"]="$(systemctl is-enabled "$unit" 2>/dev/null || true)"
  timer_active_before["$unit"]="$(systemctl is-active "$unit" 2>/dev/null || true)"
done
for unit in "${units[@]}"; do
  if [ -f "/etc/systemd/system/$unit" ]; then
    cp -a -- "/etc/systemd/system/$unit" "$rollback_root/$unit"
  else
    : > "$rollback_root/$unit.absent"
  fi
done
rollback() {
  local code=$?
  trap - ERR
  if [ "$transaction_started" -eq 1 ]; then
    if [ -n "$previous_current" ]; then
      ln -sfn "$previous_current" "$INSTALL_ROOT/.current.rollback"
      mv -Tf -- "$INSTALL_ROOT/.current.rollback" "$INSTALL_ROOT/current"
    else
      rm -f -- "$INSTALL_ROOT/current"
    fi
    rm -f -- "$launcher"
    if [ -e "$rollback_root/launcher" ] || [ -L "$rollback_root/launcher" ]; then
      cp -a -- "$rollback_root/launcher" "$launcher"
    fi
    if [ -n "$old_release" ] && [ -d "$old_release" ]; then
      [ ! -e "$release" ] || mv -- "$release" "$rollback_root/failed-release"
      mv -- "$old_release" "$release"
    fi
    for unit in "${units[@]}"; do
      if [ -f "$rollback_root/$unit" ]; then
        install -o root -g root -m 0644 "$rollback_root/$unit" "/etc/systemd/system/$unit"
      else
        rm -f -- "/etc/systemd/system/$unit"
      fi
    done
    systemctl daemon-reload || true
    for unit in pcloud-migration-supervisor.timer proton-progress-monitor.timer; do
      systemctl stop "$unit" || true
      systemctl unmask "$unit" || true
      systemctl disable "$unit" || true
      case "${timer_enabled_before[$unit]}" in
        enabled) systemctl enable "$unit" || true;;
        enabled-runtime) systemctl enable --runtime "$unit" || true;;
        masked) systemctl mask "$unit" || true;;
        masked-runtime) systemctl mask --runtime "$unit" || true;;
      esac
      case "${timer_active_before[$unit]}" in active|activating) systemctl start "$unit" || true;; esac
    done
  fi
  printf 'installation failed; rollback restored the previous release and units\n' >&2
  exit "$code"
}
trap rollback ERR
transaction_started=1
if [ -e "$release" ]; then
  old_release="$rollback_root/previous-release"
  mv -- "$release" "$old_release"
fi
mv -- "$stage" "$release"
trap - EXIT
next_link="$INSTALL_ROOT/.current.$VERSION"
ln -s "$release" "$next_link"
mv -Tf -- "$next_link" "$INSTALL_ROOT/current"
switched=1
install -d -o root -g root -m 0755 /usr/local/bin
ln -sfn "$INSTALL_ROOT/current/bin/pcloud-proton-migrate" /usr/local/bin/.pcloud-proton-migrate.new
mv -Tf -- /usr/local/bin/.pcloud-proton-migrate.new "$launcher"

for unit in pcloud-migration-supervisor.service proton-progress-monitor.service; do
  temporary="/etc/systemd/system/.$unit.$VERSION.tmp"
  escaped_config=$(printf '%s' "$CONFIG_DIR/migration.env" | sed 's/[&|\\]/\\&/g')
  sed -e "s/@PCM_RUNTIME_USER@/$PCM_RUNTIME_USER/g" -e "s|@PCM_CONFIG_FILE@|$escaped_config|g" \
    "$release/deploy/systemd/$unit" > "$temporary"
  chown root:root "$temporary"; chmod 0644 "$temporary"
  mv -f -- "$temporary" "/etc/systemd/system/$unit"
done
for unit in pcloud-migration-supervisor.timer proton-progress-monitor.timer; do
  temporary="/etc/systemd/system/.$unit.$VERSION.tmp"
  install -o root -g root -m 0644 "$release/deploy/systemd/$unit" "$temporary"
  mv -f -- "$temporary" "/etc/systemd/system/$unit"
done
systemctl daemon-reload
"$INSTALL_ROOT/current/bin/pcloud-proton-migrate" --config "$CONFIG_DIR/migration.env" preflight --offline
if [ "$ENABLE" -eq 1 ] || [ "$START" -eq 1 ]; then
  "$INSTALL_ROOT/current/bin/pcloud-proton-migrate" --config "$CONFIG_DIR/migration.env" source account status
  "$INSTALL_ROOT/current/bin/pcloud-proton-migrate" --config "$CONFIG_DIR/migration.env" compatibility probe
fi
if [ "$ENABLE" -eq 1 ]; then systemctl enable pcloud-migration-supervisor.timer proton-progress-monitor.timer; fi
if [ "$START" -eq 1 ]; then systemctl start pcloud-migration-supervisor.timer proton-progress-monitor.timer; fi
trap - ERR
printf 'installed release %s for PCM_RUNTIME_USER=%s; enable=%s start=%s\n' "$release" "$PCM_RUNTIME_USER" "$ENABLE" "$START"
