# Verification

Final proof combines one frozen pCloud manifest, exact local reconciliation and locally calculated SHA1, frozen-source freshness, same-snapshot upload acceptance, independent Proton inventory, exact destination reconciliation, expected HMAC-keyed account fingerprints, the pinned version/interface probe, and foreground controller health.

## Complete immutable tuple

Every phase and final artifact must bind the same toolkit release, bundle SHA256, Python version, rclone version, exact Proton version/build and compatibility result, snapshot ID, snapshot digest algorithm and digest, snapshot generation, frozen UTC time, source account fingerprint, and destination account fingerprint. Compatibility includes a non-mutating destination-root filesystem JSON listing parsed by the same contract as verification and recorded as `filesystem_interface_compatible=true`; creation/upload behavior is proven only by later additive phases. Destination fields may be absent only before destination binding occurs; they are mandatory thereafter. Equality of snapshot ID alone does not prove equality of this tuple.

## Exact mismatch taxonomy

| Machine key | Meaning |
| --- | --- |
| `missing` | Expected entry is absent |
| `unexpected` | Entry exists outside the frozen expected set |
| `type` | File/directory type differs |
| `size` | Claimed file size differs |
| `sha1` | SHA1 differs or required claimed SHA1 is absent |
| `mtime` | Normalized modification time differs or is absent |

Duplicate destination names and unreadable names are observation blockers. They cannot be folded into another class or ignored. Local and destination reports use the same six names; aliases such as `extra` and `modification-time` are not part of the public evidence contract.

## Completion predicates

~~~text
expected source and destination account fingerprints match
toolkit/bundle/Python/rclone/exact Proton version and compatibility probe match
snapshot ID/digest algorithm/digest/generation/frozen time match in every artifact
source was frozen before download and remains fresh
local missing/unexpected/type/size/sha1/mtime == 0
upload accepted units/directories/files/bytes == expected
upload failed/exhausted/auth/quota states == 0
destination status == verified
destination directories/files/bytes complete == expected
destination missing/unexpected/type/size/sha1/mtime == 0
duplicate/unreadable-name blockers == 0
all foreground controller phases exited successfully
ATTENTION_REQUIRED.json absent
~~~

Commands:

~~~bash
pcloud-proton-migrate --config /etc/pcloud-proton-migrate/migration.env destination verify start
pcloud-proton-migrate --config /etc/pcloud-proton-migrate/migration.env destination verify status
pcloud-proton-migrate --config /etc/pcloud-proton-migrate/migration.env completion gate
~~~

Proton SHA1 is upload-client-claimed revision metadata, not an independent server-side plaintext rehash. That limitation must remain in the handoff.

## Durable handoff

Every `completion gate` attempt atomically and durably writes `reports/completion-gate-latest.json` before writing required attention. Normal evaluations include `verified`, exact predicates, tuple bindings, attention state, and evidence paths; frozen-manifest, precondition, and status-generation failures include a classified failure and `preconditions_satisfied=false`. A failed gate does not write or update final completion evidence.

After the gate passes, `reports/final-handoff.json` is atomically written with the run ID; complete version/snapshot/account tuple; source inventory and SHA1 coverage; local expected/verified totals and all six mismatch classes; upload expected/accepted units/files/directories/bytes and failure/exhaustion totals; destination totals, all classes, and duplicate/unreadable blockers; remediation evidence; quota; foreground controller state/exit results; terminal blockers; exact satisfied predicates; evidence index; limitations; and UTC completion time.

The report must be reconstructible without chat history and must not contain raw account identities, provider filenames, credentials, or host identifiers. If any field is missing, stale, blocked, or nonzero, say incomplete and name the next public recovery command. Keep staged data until reviewed cleanup approval.
