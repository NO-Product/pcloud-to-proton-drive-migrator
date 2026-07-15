# Architecture

~~~text
pCloud (immutable source)
  -> inventory + SHA1 -> frozen snapshot and source account fingerprint
  -> persistent local staging and exact local verification
  -> additive Proton upload and destination account fingerprint
  -> exact destination verification -> durable handoff
~~~

Inventory and checksum enrichment must finish before freeze. Freeze binds canonical entries and the complete immutable tuple: toolkit/bundle, Python/rclone, exact Proton version and compatibility result, snapshot ID/digest algorithm/digest/generation/frozen UTC time, and HMAC-keyed source/destination account fingerprints as each becomes available. Release-documented filesystem and additive-upload expectations are proven when later phases exercise them. Download, local reconciliation, upload planning, upload acceptance, destination verification, and handoff reject any missing or different tuple member.

The local lifecycle is additive: create missing staged objects, verify `missing`, `unexpected`, `type`, `size`, `mtime`, and `sha1`, perform a read-only freshness comparison against the frozen source, and retain stage until final review. Source changes after freeze make the run stale; they never silently update the snapshot.

## Process ownership

Systemd must own each production long-running phase as a foreground process. A service must not launch with `nohup`, `setsid`, append `&`, call `disown`, or report success while its phase continues outside the unit. Timers with `Persistent=true` catch missed timer activations after reboot; they do not make detached children safe. An attended public CLI command may remain foreground inside `tmux` for SSH disconnect continuity, but tmux is not the production reboot controller.

## Durable state

| Artifact | Role |
| --- | --- |
| frozen manifest database | Immutable snapshot and source fingerprint |
| `operations/` | Foreground phase state, attempts, logs, and events |
| `reports/download-completion-audit.json` | Same-snapshot local gate |
| `proton-upload/upload.sqlite` | Same-snapshot upload plan and acceptance |
| `proton-verification/verify.sqlite` | Destination checkpoints and exact mismatches |
| `ATTENTION_REQUIRED.json` | Atomic manual-action contract |
| final handoff report | Atomic, durable completion decision and evidence index |

Account fingerprints are domain-separated HMAC-SHA-256 representations of canonical provider account identifiers. Raw expected identities remain only in ignored, access-controlled host configuration and travel to helpers only through stdin. The fingerprint key remains only in its separate protected configured file.
