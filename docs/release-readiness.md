# Release readiness

Release only from a reviewed, clean tracked tree at the intended commit and ref. Inspect generated source archives before publication, and exclude credentials, provider data, runtime state, expected account identities, fingerprint keys, personal paths, and unredacted evidence.

## Required checks

- Run the offline unit and contract suite for the release candidate.
- Confirm account identity and fingerprint enforcement, exact tool-version evidence, foreground systemd phase ownership, canonical mismatch keys, and atomic durable handoff generation.
- Confirm repository metadata, documentation links, supported Python bounds, and the exact Proton CLI compatibility statement match the release.
- Confirm the release ref identifies the reviewed commit and generated artifacts reproduce that tree.
- Inspect release artifacts independently for secrets and excluded runtime or provider material.

Normal CI scans the checked-out tree only and intentionally does not inspect provider state, hosts, or runtime evidence. Those environments require separate operational verification through the public status and completion-gate commands.

Installer-created `BUNDLE-SHA256SUMS` proves only that the installed copy matches the selected local source tree. It is not signed-release authenticity. Published artifacts require separately authenticated provenance.
