# Repository agent contract

This toolkit copies a frozen pCloud snapshot to Proton Drive with durable evidence.

## Absolute rules

- Treat pCloud as immutable. Permit only listing, metadata/checksum reads, content reads, and downloads.
- Never delete, trash, purge, replace, overwrite, rename, or move pre-existing Proton data.
- Use repository entry points, not improvised long-running provider commands.
- Preserve manifests, databases, reports, logs, operation directories, and staged data.
- Never expose credentials, OAuth payloads, keyring data, private keys, host/account identifiers, account-fingerprint key-file contents, or personal data.
- Keep expected account identities only in ignored host configuration and the HMAC key only in its protected configured file. Raw identities travel to helpers through stdin, never argv; report keyed fingerprints only.
- Managed systemd phases must run in the foreground and remain owned by their service.

## Lifecycle and completion

Inventory and checksum enrichment precede source freeze. Download, local reconciliation, local SHA1, upload, and destination verification must all bind to that frozen snapshot. Upload acceptance is not completion.

Claim completion only when one snapshot and the expected source/destination account fingerprints bind every phase; local and destination `missing`, `unexpected`, `type`, `size`, `sha1`, and `mtime` mismatches are zero; ambiguous/unreadable entries are absent; expected files/directories/bytes are complete; no exhausted/error/auth/quota state exists; `ATTENTION_REQUIRED.json` is absent; and the durable handoff report records a satisfied completion gate.

## Operational session start

~~~bash
CLI=/usr/local/bin/pcloud-proton-migrate
CONFIG=/etc/pcloud-proton-migrate/migration.env
$CLI --config "$CONFIG" overall status --json
$CLI --config "$CONFIG" source account status
$CLI --config "$CONFIG" compatibility probe
$CLI --config "$CONFIG" upload status
$CLI --config "$CONFIG" destination verify status
systemctl is-active pcloud-migration-supervisor.timer proton-progress-monitor.timer
~~~

Read attention and latest-status JSON when present. Report source inventory/freeze, local download, local SHA1, upload acceptance, destination verification, controller health, quota, and attention separately. Follow `docs/migration-runbook.md`, `docs/recovery.md`, and `docs/verification.md`.

Use only foreground public CLI commands. Systemd is the reboot-persistent production owner; `tmux` is acceptable for an attended foreground SSH session. Never use `nohup`, `&`, `setsid`, or `disown` for migration phases.
