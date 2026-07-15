# Operations

Production work runs from an immutable installed release against persistent storage. The public CLI and rendered systemd units are the only operational entry points. The installer checksum is local copy-integrity evidence, not release authenticity; verify release provenance separately before installation.

`pcloud-migration-supervisor.timer` schedules controller work every five minutes; `proton-progress-monitor.timer` captures durable status every twenty minutes. `Persistent=true` causes systemd to catch a missed timer activation after reboot; the active command still stays foreground so systemd owns its PID, exit code, logs, cancellation, and restart. For attended SSH work, a foreground public CLI process may run inside `tmux` and survive disconnect. Tmux does not replace systemd reboot recovery. Detached `nohup`, `setsid`, background `&`, or `disown` execution is unsupported.

After reboot, confirm the volume identity/mount, HMAC key file, reserve and quota, both account fingerprints, exact dependency/Proton interface probe, both timers, all status commands, foreground service state, and attention/latest-status JSON before resuming.

Upgrade with a new immutable version:

~~~bash
sudo scripts/migration/install-autonomous-runner.sh --version <new-release-version>
~~~

Never reuse a version string. Upgrades preserve config, schemas, databases, manifests, reports, logs, stage, events, attention history, and operation directories. Record the new bundle and tool versions; reject an upgrade that cannot read existing snapshot bindings.

Cleanup is never automatic and is outside migration completion. Never delete pCloud data or pre-existing Proton objects.
