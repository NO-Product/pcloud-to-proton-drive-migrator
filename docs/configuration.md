# Configuration

The canonical host file is `/etc/pcloud-proton-migrate/migration.env`. It is ignored operational configuration, owned by root and readable only by the runtime group. The checked-in example contains placeholders only; never commit a populated copy.

All accepted `PCM_*` keys and constraints are defined by `config/migration.env.schema`. Unknown, duplicate, missing, and empty keys are rejected. The example, schema, and parser must have the same required key set, including protected identity/fingerprint and pinned compatibility fields:

`PCM_BASE_DIR`, `PCM_STAGING_DIR`, `PCM_STORAGE_MOUNT`, `PCM_REQUIRE_MOUNTPOINT`, `PCM_SOURCE_REMOTE`, `PCM_SOURCE_ROOT`, `PCM_EXPECTED_PCLOUD_ACCOUNT`, `PCM_ACCOUNT_FINGERPRINT_KEY_FILE`, `PCM_DESTINATION_ROOT`, `PCM_EXPECTED_PROTON_ACCOUNT`, `PCM_PROTON_CLI_EXPECTED_VERSION`, `PCM_DESTINATION_CAPACITY_ACKNOWLEDGED`, `PCM_RCLONE_CONFIG`, `PCM_PROTON_RUNNER`, `PCM_PROTON_BIN`, `PCM_RUNTIME_USER`, `PCM_MIN_FREE_BYTES`, `PCM_MIN_FREE_INODES`, `PCM_INVENTORY_WORKERS`, `PCM_CHECKSUM_WORKERS`, `PCM_DOWNLOAD_TRANSFERS`, `PCM_DOWNLOAD_CHECKERS`, `PCM_MULTI_THREAD_STREAMS`, `PCM_LOCAL_VERIFY_WORKERS`, `PCM_UPLOAD_WORKERS`, `PCM_DESTINATION_VERIFY_WORKERS`, `PCM_RCLONE_RETRIES`, `PCM_RCLONE_LOW_LEVEL_RETRIES`, `PCM_RETRY_SLEEP`, `PCM_MAX_PHASE_ATTEMPTS`, and `PCM_PROGRESS_INTERVAL`.

Paths must point to persistent storage, a read-only pCloud rclone remote, the Proton runner/binary, and a dedicated runtime user. Begin with conservative concurrency and preserve byte/inode reserve headroom.

## Account binding

`PCM_EXPECTED_PCLOUD_ACCOUNT` and `PCM_EXPECTED_PROTON_ACCOUNT` are required host-local assertions. Raw immutable account IDs are preferred over email addresses. Populated values exist only in ignored configuration; the checked-in example contains placeholders. They are not exported, logged, written to status/evidence, or passed through child argv. Public source/destination account probes pass each value to the identity helper through stdin.

`PCM_ACCOUNT_FINGERPRINT_KEY_FILE` names a separate persistent file containing at least 32 random bytes. It must be readable only by the required root/runtime principals, must never be sourced as environment text, and must be preserved securely for the whole run and handoff lifetime. Derive domain-separated HMAC-SHA-256 provider/account fingerprints with this key. Rotation changes fingerprints and requires a reviewed new run/snapshot; never put key bytes in config, argv, logs, evidence, or support output.

At every provider boundary, compare the authenticated account to the configured expectation. Freeze the source fingerprint into the snapshot; bind the destination fingerprint into upload, verification, and handoff evidence. A missing or changed identity is a hard stop, not an authentication retry.

## Compatibility policy

- Python `>=3.11,<3.14` is required.
- `PCM_PROTON_CLI_EXPECTED_VERSION` pins the exact `--version` output; there is no blanket version-range promise and no separate interface-contract config key.
- The release expects account login/info JSON, filesystem list JSON, folder creation, and additive upload conflict behavior. These expectations are documentation and code-level interfaces, not a configurable identifier.
- Run `pcloud-proton-migrate --config PATH compatibility probe` before activation. The non-mutating probe rejects exact-version/account-info drift and executes the verifier's read-only `filesystem list -j` parser against `PCM_DESTINATION_ROOT`; success requires `filesystem_interface_compatible=true`. Folder creation and additive conflict handling remain bound only when later phases safely exercise them.
- Record toolkit release/bundle SHA256, Python, rclone, exact Proton version/build, and probe result in durable evidence before source freeze.
- `config validate` proves parser/schema syntax and value constraints only. `doctor` proves executable/path/provider/capacity facts. Runtime completion additionally proves that the required account, additive upload, and JSON listing interfaces were exercised successfully.

## Capacity and quota

`PCM_MIN_FREE_BYTES` and `PCM_MIN_FREE_INODES` are reserves, not dataset sizing. Include payload, temporary files, databases, logs, partial work, and recovery headroom. Source account used quota may include shared, linked, virtual, or out-of-scope data and is not a substitute for frozen inventory totals.

Check destination quota before upload and during recovery. On local capacity or Proton quota exhaustion, stop, write attention state, preserve all state, expand capacity/quota, rerun `doctor`, and resume the same checkpoint. Never create space by deleting source, stage, evidence, or destination objects.
