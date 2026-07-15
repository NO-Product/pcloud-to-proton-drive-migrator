# Limitations

- Python 3.11 through 3.13 is supported; Python 3.14 and newer are outside the package bound.
- Proton CLI compatibility is exact-build and interface-based; no blanket version range is supported.
- Some Proton CLI/keyring combinations cannot maintain a session for a non-login systemd user.
- pCloud creation time is inventoried but cannot currently be set through the Proton CLI.
- Proton revision metadata can represent original file mtime; node timestamps may reflect upload activity.
- Proton SHA1 is client-claimed encrypted revision metadata, not an independent server-side plaintext rehash.
- Folder timestamp behavior differs from file revision metadata.
- Active Proton upload bytes are not trustworthy accepted-byte progress.
- Source account used quota may include scopes outside the selected tree.
- Local capacity and destination quota can stop a run and require expansion; no automatic deletion is permitted.
- Authentication expiry, rate limits, MIME handling, literal names, duplicate/unreadable entries, and provider interface drift can block automation.
- Systemd cannot notify or wake a conversational agent.
- Cleanup is not automatic and is not part of completion.

Any unresolved limitation affecting a predicate, stale evidence, identity/version drift, or missing durable handoff keeps the migration incomplete.
