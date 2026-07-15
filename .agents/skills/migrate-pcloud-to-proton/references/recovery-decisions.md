# Recovery decisions

| State | Public action after classification/correction |
| --- | --- |
| Inventory interrupted | `pcloud-proton-migrate --config PATH source inventory` |
| Checksums interrupted | `pcloud-proton-migrate --config PATH source checksums` |
| Ready to freeze | `pcloud-proton-migrate --config PATH source freeze` |
| Download interrupted | `pcloud-proton-migrate --config PATH download resume` |
| Local verify interrupted | `pcloud-proton-migrate --config PATH local verify resume` |
| Internal controller phase interrupted | `pcloud-proton-migrate --config PATH supervise` |
| Upload `recoverable` | `pcloud-proton-migrate --config PATH upload resume` |
| Upload `complete` | `pcloud-proton-migrate --config PATH destination verify start` |
| Verify `recoverable` | `pcloud-proton-migrate --config PATH destination verify resume` |
| `blocked-authentication` | Run `auth login`, verify unchanged fingerprint, then phase resume |
| Capacity/quota exhausted | Expand, run `doctor`, then phase resume; delete nothing |
| Identity/version mismatch | Stop; restore expected account/build or start reviewed new evidence |
| Compatibility probe failed | Stop; install the pinned Proton build or update the release contract after review |
| Verification mismatch/blocker | Report exact class; do not alter destination extras |
| Candidate `verified` | `pcloud-proton-migrate --config PATH completion gate` |

Read attention phase/reason/action and referenced evidence. Run recovery as a foreground public command under systemd or inside attended `tmux`; never use `nohup` or shell detachment. Never remove attention manually, mark failed units complete, or alter pre-existing destination data.
