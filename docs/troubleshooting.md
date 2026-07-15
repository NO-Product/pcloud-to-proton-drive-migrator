# Troubleshooting

| Symptom | Safe action |
| --- | --- |
| Fresh installer exits `78` | After package/user bootstrap, edit the template, create the HMAC key file, run configured storage bootstrap, then install |
| `config validate` succeeds but `doctor` fails | Correct the named executable, remote, mount, capacity, or runtime-user condition |
| Proton login works only interactively | Use a compatible protected keyring/session; do not use empty passwords or export credentials |
| Authenticated account fingerprint differs | Stop; select the configured account and re-authenticate |
| Proton version/interface probe differs | Stop; install the pinned exact build or review a new release contract |
| Source changed after freeze | Mark the run stale; do not update downstream evidence in place |
| Service exits while a phase PID remains | Treat as unsupported detached execution and controller failure |
| Upload reaches 100 percent | Start/inspect destination verification; do not claim completion |
| Local disk or Proton quota exhausted | Expand capacity/quota, preserve everything, rerun `doctor`, resume checkpoint |
| `blocked-authentication` | Run public `auth login`, confirm the same fingerprint, then phase resume |
| Unexpected destination entries | Record `unexpected`; never move or delete them |
| Duplicate or unreadable destination name | Stop for manual classification; do not coerce it into another class |
| Verification interrupted | Run `destination verify resume`; preserve its database |
| Attention remains | Follow its exact required action; never delete it manually |
| SSH session must disconnect | Keep the public command foreground inside `tmux`, or use systemd; never use `nohup`/background detachment |

A support bundle contains only sanitized status, timestamp, error class, public command, software versions, and compatibility result. Exclude paths, names, manifests, identities/fingerprints, OAuth material, tokens, keys, email addresses, device/host data, IP addresses, and raw keyring/log output.
