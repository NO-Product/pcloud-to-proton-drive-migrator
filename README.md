# pCloud to Proton Drive migration toolkit

A resumable, evidence-driven toolkit for copying a frozen pCloud snapshot through persistent local staging into Proton Drive.

> Upload acceptance is not migration completion. Completion requires local proof, accepted upload evidence, and zero-mismatch destination reconciliation bound to the same frozen snapshot and account fingerprints.

## Safety and lifecycle

- pCloud is immutable: only listing, metadata/checksum reads, content reads, and downloads are permitted.
- Proton is additive: pre-existing destination objects are never deleted, trashed, replaced, overwritten, renamed, or moved.
- Inventory and SHA1 enrichment finish before the source snapshot is frozen; download and all local work are bound to that frozen snapshot.
- Checkpoints, manifests, reports, logs, databases, and staged data survive interruption and reboot.
- Local capacity or destination quota exhaustion stops safely, preserves evidence and staged data, and resumes only after capacity is expanded.
- Expected source and destination identities exist only in ignored host configuration. The HMAC fingerprint key is a separate protected file named by configuration. Public probes consume raw expectations through stdin, never argv; durable evidence contains only domain-separated HMAC-SHA-256 account fingerprints.

~~~text
inventory -> checksums -> freeze
  -> download -> local path/type/size/mtime reconciliation -> local SHA1
  -> frozen-source freshness check
  -> upload plan and acceptance
  -> destination inventory and reconciliation
  -> durable handoff report
~~~

The canonical machine mismatch classes are `missing`, `unexpected`, `type`, `size`, `sha1`, and `mtime`. Ambiguous duplicate entries and unreadable names are verification blockers. Every class must be zero.

## Fresh-host outline

~~~bash
sudo scripts/bootstrap/debian-ubuntu.sh
sudo scripts/migration/install-autonomous-runner.sh --version <release-version>
# Expected first-run exit 78: edit /etc/pcloud-proton-migrate/migration.env.
sudo scripts/bootstrap/debian-ubuntu.sh --config /etc/pcloud-proton-migrate/migration.env
sudo scripts/migration/install-autonomous-runner.sh --version <release-version>

CLI=/usr/local/bin/pcloud-proton-migrate
CONFIG=/etc/pcloud-proton-migrate/migration.env
$CLI --config "$CONFIG" config validate
$CLI --config "$CONFIG" doctor
$CLI --config "$CONFIG" source account status
$CLI --config "$CONFIG" compatibility probe
$CLI --config "$CONFIG" auth login
$CLI --config "$CONFIG" auth status

# Starting is a separate, explicit decision after all checks and authentication.
sudo systemctl enable --now pcloud-migration-supervisor.timer proton-progress-monitor.timer
~~~

The installer does not enable or start services by default. Its generated `BUNDLE-SHA256SUMS` self-check proves local copy integrity only; it is not a signature or release-origin proof. Managed phases stay in the foreground under systemd. An attended SSH run may stay foreground inside `tmux`; `nohup`, shell backgrounding, `setsid`, and `disown` are unsupported. See the [installation guide](docs/installation.md) and [migration runbook](docs/migration-runbook.md).

## Compatibility

Python 3.11 through 3.13 is supported. Proton Drive CLI compatibility is interface-based and release-specific: configure the exact expected CLI version/build, require an exact match, and run the release's non-mutating interface probe before authentication or activation. Record the complete toolkit bundle, Python, rclone, Proton version/interface, frozen snapshot, and keyed-account tuple in durable evidence and the handoff report.

## Documentation

- [Documentation index](docs/README.md)
- [Configuration](docs/configuration.md)
- [Recovery](docs/recovery.md)
- [Verification](docs/verification.md)
- [Release readiness](docs/release-readiness.md)

## Support

Use repository Discussions only if enabled for usage questions, and Issues only if enabled for sanitized reproducible defects. Follow [SUPPORT.md](SUPPORT.md); never disclose provider data, identities, credentials, manifests, or unredacted logs.
