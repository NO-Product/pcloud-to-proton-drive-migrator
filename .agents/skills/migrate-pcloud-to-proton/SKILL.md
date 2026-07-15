---
name: migrate-pcloud-to-proton
description: Bootstrap, operate, monitor, recover, verify, and hand off the repository's resumable pCloud-to-Proton Drive migration on a fresh host without relying on chat history.
---

# Migrate pCloud to Proton

Treat pCloud as immutable and Proton as additive. Never alter pre-existing destination objects. Never delete checkpoints, evidence, or staged data. Upload acceptance is not completion.

## 1. Gather non-secret inputs

Confirm release/version, persistent mount and reserves, runtime root/user, read-only pCloud remote/root, dedicated Proton account, destination/quota, Python/rclone versions, the exact expected Proton CLI version/interface contract, protected keyring/session, expected account identities, protected HMAC fingerprint key-file path, and approved concurrency. Expected identities belong only in ignored host config; key bytes belong only in the separate protected file. Never request or repeat secrets, raw identities, provider paths, or personal data in chat.

## 2. Bootstrap a fresh host

~~~bash
sudo scripts/bootstrap/debian-ubuntu.sh
sudo scripts/migration/install-autonomous-runner.sh --version <release-version>
~~~

The first bootstrap installs prerequisite packages and the default runtime account only; it does not require configured storage and starts nothing. Then expect installer exit `78` after `/etc/pcloud-proton-migrate/migration.env` is installed. Edit every placeholder, create the protected HMAC key file, and pin the exact Proton version/interface contract. Do not add unknown keys; if the schema lacks these fields, stop and report a blocker.

~~~bash
sudo scripts/bootstrap/debian-ubuntu.sh --config /etc/pcloud-proton-migrate/migration.env
sudo scripts/migration/install-autonomous-runner.sh --version <release-version>
~~~

The configured bootstrap provisions persistent directories, ownership, and permissions. The second installer deploys but does not enable/start by default. Use `--repair` only for an intentional same-version repair. Its locally generated checksum self-check proves copied-file integrity, not signed release authenticity. Do not add activation flags yet.

## 3. Prove compatibility and session

Python must be `>=3.11,<3.14`. Proton compatibility is exact-build and interface-based, never blanket support. Require the configured Proton version/build to match and run the release's non-mutating account/filesystem JSON interface probe. Runtime folder creation and additive conflict behavior remain later exercised evidence, never permission to mutate during the probe.

Use a protected keyring/session usable by the runtime user and foreground systemd services. Never use empty-password recipes or credentials in config/environment text.

~~~bash
CLI=/usr/local/bin/pcloud-proton-migrate
CONFIG=/etc/pcloud-proton-migrate/migration.env
$CLI --config "$CONFIG" config validate
$CLI --config "$CONFIG" doctor
$CLI --config "$CONFIG" source account status
$CLI --config "$CONFIG" auth login
$CLI --config "$CONFIG" compatibility probe
$CLI --config "$CONFIG" auth status
~~~

The public identity commands read raw expected identities from protected config and supply them to helpers through stdin; never place them on argv or print them. Compare both accounts with ignored expectations and persist only domain-separated HMAC-SHA-256 fingerprints. Authentication without identity equality is failure.

## 4. Inspect before starting

~~~bash
$CLI --config "$CONFIG" overall status --json
$CLI --config "$CONFIG" upload status
$CLI --config "$CONFIG" destination verify status
systemctl is-active pcloud-migration-supervisor.timer proton-progress-monitor.timer
~~~

Read attention/latest-status JSON when present. Report source inventory/freeze, local download/reconciliation/SHA1, freshness, upload acceptance, destination verification, quota, foreground controller health, and attention separately.

## 5. Explicitly opt in

Only after config, doctor, compatibility, identity, keyring/session, persistent mount, capacity, and destination quota pass:

~~~bash
sudo systemctl enable --now pcloud-migration-supervisor.timer proton-progress-monitor.timer
~~~

Managed long-running phases must remain foreground systemd processes. For an attended SSH session, run the public foreground command inside `tmux`. Never use `nohup`, `&`, `setsid`, or `disown`; tmux survives disconnect, while systemd is the supported reboot-persistent owner.

## 6. Enforce lifecycle

The controller must inventory, checksum, and freeze before download. It then performs exact local reconciliation/SHA1 and frozen-source freshness, upload planning/acceptance, destination inventory, and exact verification. Required machine mismatches are `missing`, `unexpected`, `type`, `size`, `sha1`, and `mtime`; duplicate and unreadable-name observations are blockers.

## 7. Monitor and recover

Use real numerators/denominators, timestamps, accepted versus active bytes, failure classes, quota headroom, account fingerprints, and systemd ownership. Read [recovery decisions](references/recovery-decisions.md). Correct only classified causes and use public resume commands. On capacity/quota, expand without deletion; on auth renewal, require the same fingerprint.

## 8. Verify and hand off

Read [evidence contract](references/evidence-contract.md). Run destination status and `completion gate`. Say complete only after every predicate passes and `reports/final-handoff.json` atomically records the complete version/snapshot/account tuple, counts/classes, quota, controller results, evidence paths, limitations, attention absence, UTC time, and exact predicates. Otherwise use `reports/completion-gate-latest.json`, say incomplete, and give the exact public recovery command.

Keep stage and all evidence until user-reviewed cleanup approval. Cleanup is separate from completion.
