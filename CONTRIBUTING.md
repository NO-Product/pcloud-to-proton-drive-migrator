# Contributing

Contributions are accepted under Apache-2.0.

- Keep Python support bounded to 3.11 through 3.13 and runtime dependencies explicit.
- Do not claim Proton CLI compatibility by version family. Document and test the exact account/filesystem interfaces used, and record the tested CLI version.
- Keep pCloud operations read-only and Proton recovery additive and non-destructive.
- Keep long-running systemd work in the foreground, resumable, and evidence-preserving.
- Keep raw expected accounts off argv and account fingerprints HMAC-keyed from a protected key file.
- Never include credentials, runtime state, expected account identities, fingerprint keys, personal paths, provider data, or unredacted evidence.
- Add unit and static contract coverage for behavior and safety changes.

Run `make check` before submitting. This runs all unit and contract tests without provider or network access. Describe operational guarantees, recovery behavior, compatibility evidence, and any schema transition.
