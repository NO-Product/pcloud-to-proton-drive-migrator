# Recovery

Preserve checkpoints and classify first. Never recover by deleting or editing state, source, stage, evidence, attention files, or existing destination objects.

| State | Public safe action after correcting the cause |
| --- | --- |
| Source inventory interrupted | `pcloud-proton-migrate --config PATH source inventory` |
| Source checksum enrichment interrupted | `pcloud-proton-migrate --config PATH source checksums` |
| Frozen snapshot absent after complete inventory/checksums | `pcloud-proton-migrate --config PATH source freeze` |
| Download interrupted | `pcloud-proton-migrate --config PATH download resume` |
| Local verification interrupted | `pcloud-proton-migrate --config PATH local verify resume` |
| Internal metadata/freshness controller phase interrupted | `pcloud-proton-migrate --config PATH supervise` |
| Upload `recoverable` | `pcloud-proton-migrate --config PATH upload resume` |
| Upload `complete` | `pcloud-proton-migrate --config PATH destination verify start` |
| Destination verification `recoverable` | `pcloud-proton-migrate --config PATH destination verify resume` |
| `blocked-authentication` | `pcloud-proton-migrate --config PATH auth login`, then the phase resume command |
| Upload or verification `blocked-quota` | Expand Proton quota, rerun `doctor`, confirm account identity, then resume the same phase |
| Candidate `verified` | `pcloud-proton-migrate --config PATH completion gate` |

For `ATTENTION_REQUIRED.json`, read `phase`, `reason`, `required_action`, timestamp, and referenced evidence. Correct only that condition and use the listed public entry point; never remove the file manually.

Local capacity and Proton quota are terminal attention boundaries until expanded. Preserve partial work, expand capacity/quota, rerun `doctor`, confirm the expected account fingerprints, then resume the same checkpoint. Never free capacity by deleting migration evidence, staged data, source data, or destination data.

Authentication renewal must retain the same expected account fingerprint. A different account is an identity mismatch and hard stop. Unknown, permission, ambiguity, exhausted-attempt, duplicate, unreadable-name, or verification mismatch states require review; never mark failed work complete to improve counters.

Run every recovery through the public CLI in the foreground. Use systemd for reboot-persistent ownership or `tmux` for an attended foreground SSH session. Never use `nohup`, shell backgrounding, `setsid`, or `disown`; after key loss/rotation or a pinned Proton version/interface mismatch, stop rather than rebinding existing evidence.
