# Script scope contract

These rules supplement the root contract.

- Keep pCloud calls provably list/read/checksum/download-only.
- Use Proton file conflict `skip` and folder conflict `merge`; never alter a pre-existing object.
- Route long work through `scripts/migration` entry points with durable SQLite/JSON state.
- Bound retries, classify terminal failures, and write `ATTENTION_REQUIRED.json`.
- Keep status lightweight and free of provider traversal.
- Preserve schemas and evidence across upgrades.
- Never embed credentials, host/account identifiers, or empty-password keyring recipes.
- Upload `complete` must lead to destination verification, never a completion claim.
