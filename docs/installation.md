# Installation

## Prerequisites

- Debian or Ubuntu with systemd and persistent mounted storage
- Python 3.11 through 3.13, `sqlite3`, `jq`, `flock`, `rclone`, `tmux` for optional attended SSH continuity, and required base utilities
- Dedicated read-only pCloud remote and dedicated Proton migration account
- Exact Proton Drive CLI build tested against this release's required interfaces
- Protected keyring/session usable by the non-login runtime user and foreground systemd services
- Sufficient local reserve and destination quota

Some Proton CLI builds assume an interactive desktop keyring or change JSON/command interfaces. Such a build is incompatible until its exact interfaces and noninteractive session behavior are proven. Never use an empty-password keyring recipe or place login material in the environment file.

## Fresh host, non-starting by default

1. Bootstrap prerequisite packages and the default runtime account. This stage does not require the storage config, provision migration storage, install services, or start work.

~~~bash
sudo scripts/bootstrap/debian-ubuntu.sh
~~~

2. Run the installer once to place the canonical template:

~~~bash
sudo scripts/migration/install-autonomous-runner.sh --version <release-version>
~~~

On a fresh host this installs `/etc/pcloud-proton-migrate/migration.env` with mode `0600` and intentionally exits `78`. This is expected; no release or service is deployed.

3. Edit every placeholder. Configure the persistent mount, runtime user, read-only remote, destination, quota reserves, `PCM_PROTON_CLI_EXPECTED_VERSION`, and conservative concurrency. The release-documented Proton interface expectations are not a separate config key. Keep raw expected identities only in this ignored file. Create the separate configured HMAC fingerprint key file with at least 32 random bytes and restrictive ownership/mode; preserve it outside staged payload and never print it.

4. Rerun bootstrap with configuration to provision and verify persistent storage, directories, ownership, and permissions. This still does not install, enable, or start services.

~~~bash
sudo scripts/bootstrap/debian-ubuntu.sh --config /etc/pcloud-proton-migrate/migration.env
~~~

5. Run the installer a second time without activation flags. It installs one immutable versioned bundle, preserves config, renders units, and reloads systemd. It still does not enable or start anything.

~~~bash
sudo scripts/migration/install-autonomous-runner.sh --version <release-version>
~~~

If and only if an existing bundle with the same version is being intentionally repaired, add `--repair`; normal upgrade uses a new version. The install transaction snapshots the prior `/usr/local/bin/pcloud-proton-migrate` state as an exact symlink target, regular file, or absence and restores it on deployment, offline-preflight, or opt-in activation failure. `BUNDLE-SHA256SUMS` is generated from the local source tree and checked after copying. This detects local copy damage only and is not a signature, provenance check, or proof that the checkout/release is authentic.

6. Validate configuration and host facts before provider work.

~~~bash
CLI=/usr/local/bin/pcloud-proton-migrate
CONFIG=/etc/pcloud-proton-migrate/migration.env
$CLI --config "$CONFIG" config validate
$CLI --config "$CONFIG" doctor
~~~

7. Probe the source identity. Public identity handling reads the configured expectation and supplies it to the helper through stdin; do not reproduce raw identities on a command line.

~~~bash
$CLI --config "$CONFIG" source account status
~~~

8. Establish the protected Proton keyring/session interactively through the public wrapper, then run the non-mutating exact-version/account-JSON and destination-root `filesystem list -j` compatibility probe. The probe validates the verifier's JSON-array contract and emits `filesystem_interface_compatible`; it never creates, moves, uploads, or deletes destination data.

~~~bash
$CLI --config "$CONFIG" auth login
$CLI --config "$CONFIG" compatibility probe
$CLI --config "$CONFIG" auth status
~~~

Confirm both provider identities match ignored configuration and capture only HMAC-keyed fingerprints. Authentication success alone is not identity proof.

9. Inspect initial status. Starting remains a separate opt-in decision.

~~~bash
$CLI --config "$CONFIG" overall status --json
systemctl is-enabled pcloud-migration-supervisor.timer proton-progress-monitor.timer
~~~

10. Only after validation, doctor, both identity probes, compatibility, keyring/session, mount, capacity, and quota gates pass, opt in:

~~~bash
sudo systemctl enable --now pcloud-migration-supervisor.timer proton-progress-monitor.timer
~~~

Long-running public commands must remain foreground children of their systemd services. For attended manual operation over SSH, keep the public CLI foreground process in `tmux`. Tmux preserves the process across terminal disconnect, but only systemd provides the supported reboot-persistent production ownership. Never use `nohup`, `&`, `setsid`, `disown`, or direct detached provider commands.
