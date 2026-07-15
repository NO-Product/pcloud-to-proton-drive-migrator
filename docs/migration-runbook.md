# Migration runbook

## Discover state first

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

Read `ATTENTION_REQUIRED.json` and latest durable status when present. Report source inventory/freeze, local download/reconciliation/SHA1, source freshness, upload acceptance, destination verification, foreground controller health, local capacity, destination quota, and attention separately.

## Required order

1. Record and bind toolkit release/bundle SHA256, Python/rclone, exact Proton version/build, account JSON, and the non-mutating destination-root filesystem-list JSON probe, then prove both HMAC-keyed account fingerprints without putting raw expectations on argv. Later phases bind folder creation and additive upload when exercised.
2. Build source inventory, enrich every file with SHA1, then freeze the snapshot before download.
3. Download against the frozen snapshot. Before rclone writes, the wrapper rejects a symlink staging root, every symlink path component, and every symlink recursively present beneath staging; then apply metadata and reconcile local `missing`, `unexpected`, `type`, `size`, `mtime`, and `sha1` to zero.
4. Confirm the live read-only source still matches the frozen snapshot. Any drift makes the run stale.
5. Bind the upload plan to snapshot and source/destination fingerprints; upload additively and record acceptance.
6. Inventory Proton independently and reconcile all exact classes to zero.
7. Run `completion gate`; every attempt first atomically and durably replaces `reports/completion-gate-latest.json`, including frozen-manifest, precondition, or status failures, and writes attention afterward when required. Write the durable handoff report only when every predicate passes.

Upload status `complete`, 100 percent, or zero failed units means acceptance only. It means destination verification is next.

## Monitor and recover

Use only public status and recovery commands from [Recovery](recovery.md). A managed phase must be a foreground systemd process; an inactive service with a supposedly running detached PID is controller failure. An attended foreground command may run inside `tmux`; never use `nohup` or shell detachment.

Retain all staged data and evidence until the user reviews the handoff report and separately approves cleanup.
