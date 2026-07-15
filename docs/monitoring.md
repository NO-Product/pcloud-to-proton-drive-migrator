# Monitoring

~~~bash
pcloud-proton-migrate --config /etc/pcloud-proton-migrate/migration.env overall status --json
pcloud-proton-migrate --config /etc/pcloud-proton-migrate/migration.env source account status
pcloud-proton-migrate --config /etc/pcloud-proton-migrate/migration.env compatibility probe
pcloud-proton-migrate --config /etc/pcloud-proton-migrate/migration.env upload status
pcloud-proton-migrate --config /etc/pcloud-proton-migrate/migration.env destination verify status
systemctl status pcloud-migration-supervisor.service proton-progress-monitor.service
~~~

Report the durable UTC timestamp and these dimensions independently:

- complete version/snapshot tuple and HMAC-keyed source account fingerprint
- local download files/bytes, exact local mismatch classes, and SHA1 coverage
- source freshness against the frozen snapshot
- upload expected/accepted units, directories, files, and bytes; active work separately
- sanitized destination account fingerprint and quota headroom
- destination expected/verified directories, files, bytes, and every mismatch class
- foreground systemd service/PID/exit health and timer health
- classified failures, exhausted attempts, and attention state

The exact machine classes are `missing`, `unexpected`, `type`, `size`, `sha1`, and `mtime`; duplicate and unreadable-name observations are blockers and must be reported separately. Do not relabel `unexpected` as `extra` or `mtime` as `modification-time` in public evidence.

No reliable intra-upload byte counter exists. Never count active bytes as accepted or interpolate smooth progress. Upload `complete` is acceptance, not completion.
