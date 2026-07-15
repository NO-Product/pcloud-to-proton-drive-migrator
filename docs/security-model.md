# Security model

pCloud is immutable. Allowed behavior is listing, metadata/checksum reads, content reads, and downloads. Upload, sync-to-source, create, rename, copy-to-source, move, trash, restore, delete, purge, and every other source mutation are forbidden.

Proton operation is additive. Existing files are skipped and existing folders may be traversed/merged for additive children; automation never deletes, trashes, purges, replaces, overwrites, renames, or moves destination objects. Unexpected objects remain in place and fail verification.

Expected provider account identities belong only in ignored, root-controlled host configuration. The HMAC-SHA-256 fingerprint key belongs only in a separate configured, persistent, access-controlled file. Public probes supply raw expectations to internal helpers through stdin, never argv. Compare identities at authentication, freeze, upload, verification, and recovery boundaries. Durable artifacts expose only domain-separated HMAC fingerprints. Never persist raw account ID/email or key bytes in manifests, databases, reports, logs, events, units, process listings, or support bundles.

Protect OAuth/browser payloads, tokens, cookies, keyrings/passwords, SSH/private keys, host/device identifiers, personal filenames, inventories, and runtime paths. Use a dedicated runtime user, protected non-empty-password keyring/session, least privilege, and restrictive permissions.

Transactional SQLite, atomic JSON, immutable snapshot binding, foreground systemd ownership, bounded retries, and preserved append-only evidence define the trust boundary. Authentication, identity mismatch, capacity/quota, compatibility drift, unknown remediation, unexpected destination data, and cleanup require a human.
