#!/usr/bin/env python3
"""Shared durable SQLite and immutable snapshot primitives."""

import contextlib
import datetime as dt
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unicodedata


MANIFEST_SCHEMA_VERSION = 4
UPLOAD_SCHEMA_VERSION = 4
VERIFY_SCHEMA_VERSION = 4
SNAPSHOT_DIGEST_ALGORITHM = "sha256-manifest-v1"
ACCOUNT_FINGERPRINT_ALGORITHM = "hmac-sha256-provider-account-v1"


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _add_column(db, table, name, definition):
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _install_manifest_freeze_triggers(db):
    protected = "'source_remote','source_account_fingerprint','source_identity','snapshot_id','snapshot_digest','snapshot_digest_algorithm','snapshot_generation','frozen_at'"
    db.execute("CREATE TRIGGER IF NOT EXISTS manifest_entries_frozen_insert BEFORE INSERT ON entries WHEN EXISTS(SELECT 1 FROM metadata WHERE key='frozen_at') BEGIN SELECT RAISE(ABORT,'source manifest is frozen'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS manifest_entries_frozen_update BEFORE UPDATE ON entries WHEN EXISTS(SELECT 1 FROM metadata WHERE key='frozen_at') BEGIN SELECT RAISE(ABORT,'source manifest is frozen'); END")
    db.execute("CREATE TRIGGER IF NOT EXISTS manifest_entries_frozen_delete BEFORE DELETE ON entries WHEN EXISTS(SELECT 1 FROM metadata WHERE key='frozen_at') BEGIN SELECT RAISE(ABORT,'source manifest is frozen'); END")
    db.execute(f"CREATE TRIGGER IF NOT EXISTS manifest_metadata_frozen_insert BEFORE INSERT ON metadata WHEN NEW.key IN ({protected}) AND EXISTS(SELECT 1 FROM metadata WHERE key='frozen_at') BEGIN SELECT RAISE(ABORT,'source identity is frozen'); END")
    db.execute(f"CREATE TRIGGER IF NOT EXISTS manifest_metadata_frozen_update BEFORE UPDATE ON metadata WHEN OLD.key IN ({protected}) AND EXISTS(SELECT 1 FROM metadata WHERE key='frozen_at') BEGIN SELECT RAISE(ABORT,'source identity is frozen'); END")
    db.execute(f"CREATE TRIGGER IF NOT EXISTS manifest_metadata_frozen_delete BEFORE DELETE ON metadata WHEN OLD.key IN ({protected}) AND EXISTS(SELECT 1 FROM metadata WHERE key='frozen_at') BEGIN SELECT RAISE(ABORT,'source identity is frozen'); END")


def _manifest_migration(db, version):
    if version == 1:
        db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS directories(path TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,started_at TEXT,completed_at TEXT,last_error TEXT,folder_id INTEGER)")
        db.execute("CREATE TABLE IF NOT EXISTS entries(path TEXT PRIMARY KEY,parent_path TEXT NOT NULL,name TEXT NOT NULL,is_dir INTEGER NOT NULL,size INTEGER NOT NULL,mod_time TEXT,md5 TEXT,sha1 TEXT,object_id TEXT,mime_type TEXT,seen_at TEXT NOT NULL,created_time TEXT,pcloud_hash TEXT,checksum_status TEXT,checksum_error TEXT,checksum_attempts INTEGER NOT NULL DEFAULT 0)")
        db.execute("CREATE INDEX IF NOT EXISTS entries_parent_idx ON entries(parent_path)")
        db.execute("CREATE TABLE IF NOT EXISTS verification(path TEXT PRIMARY KEY,status TEXT NOT NULL,detail TEXT,checked_at TEXT NOT NULL,algorithm TEXT,source_hash TEXT,local_hash TEXT,local_size INTEGER,local_mtime_ns INTEGER)")
    elif version == 2:
        _add_column(db, "directories", "folder_id", "INTEGER")
        for name, definition in {
            "created_time": "TEXT", "pcloud_hash": "TEXT", "checksum_status": "TEXT",
            "checksum_error": "TEXT", "checksum_attempts": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            _add_column(db, "entries", name, definition)
        for name, definition in {
            "algorithm": "TEXT", "source_hash": "TEXT", "local_hash": "TEXT",
            "local_size": "INTEGER", "local_mtime_ns": "INTEGER",
        }.items():
            _add_column(db, "verification", name, definition)
        _install_manifest_freeze_triggers(db)
    elif version == 3:
        for name, definition in {
            "folder_id": "INTEGER", "attempts": "INTEGER NOT NULL DEFAULT 0",
            "started_at": "TEXT", "completed_at": "TEXT", "last_error": "TEXT",
        }.items():
            _add_column(db, "directories", name, definition)
        for name, definition in {
            "parent_path": "TEXT NOT NULL DEFAULT ''", "name": "TEXT NOT NULL DEFAULT ''",
            "is_dir": "INTEGER NOT NULL DEFAULT 0", "size": "INTEGER NOT NULL DEFAULT 0",
            "mod_time": "TEXT", "md5": "TEXT", "sha1": "TEXT", "object_id": "TEXT",
            "mime_type": "TEXT", "seen_at": "TEXT", "created_time": "TEXT",
            "pcloud_hash": "TEXT", "checksum_status": "TEXT", "checksum_error": "TEXT",
            "checksum_attempts": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            _add_column(db, "entries", name, definition)
        for name, definition in {
            "status": "TEXT NOT NULL DEFAULT 'pending'", "detail": "TEXT", "checked_at": "TEXT",
            "algorithm": "TEXT", "source_hash": "TEXT", "local_hash": "TEXT",
            "local_size": "INTEGER", "local_mtime_ns": "INTEGER",
        }.items():
            _add_column(db, "verification", name, definition)
        _install_manifest_freeze_triggers(db)
    elif version == 4:
        for name in (
            "manifest_entries_frozen_insert", "manifest_entries_frozen_update", "manifest_entries_frozen_delete",
            "manifest_metadata_frozen_insert", "manifest_metadata_frozen_update", "manifest_metadata_frozen_delete",
        ):
            db.execute(f"DROP TRIGGER IF EXISTS {name}")
        values = dict(db.execute("SELECT key,value FROM metadata"))
        fingerprint = values.get("source_account_fingerprint")
        if not fingerprint:
            fields = {}
            if values.get("source_account_id"):
                fields["account_id"] = values["source_account_id"]
            if values.get("source_account_email"):
                fields["account_email"] = values["source_account_email"]
            try:
                legacy = json.loads(values.get("source_identity", "{}"))
            except json.JSONDecodeError:
                legacy = {}
            if isinstance(legacy, dict):
                for old, new in (("account_id", "account_id"), ("account_email", "account_email")):
                    if legacy.get(old) and new not in fields:
                        fields[new] = legacy[old]
            if fields or values.get("status") or values.get("frozen_at"):
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('account_binding_attention',?)",
                           ("source account identity requires a keyed fingerprint; create a new inventory with --expected-account-stdin and --fingerprint-key-file",))
        db.execute("DELETE FROM metadata WHERE key IN ('source_account_id','source_account_email')")
        if fingerprint:
            remote = unicodedata.normalize("NFC", values.get("source_remote", "").strip())
            if remote and ":" not in remote:
                remote += ":"
            identity = canonical_json({"account_fingerprint": fingerprint, "provider": "pcloud", "remote": remote})
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('source_identity',?)", (identity,))
            db.execute("DELETE FROM metadata WHERE key='account_binding_attention'")
            if values.get("frozen_at"):
                digest = manifest_digest(db, json.loads(identity))
                generation = int(values.get("snapshot_generation", 1))
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('snapshot_digest',?)", (digest,))
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('snapshot_digest_algorithm',?)",
                           (SNAPSHOT_DIGEST_ALGORITHM,))
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('snapshot_id',?)",
                           (f"pcloud-{generation}-{digest[:24]}",))
        else:
            db.execute("DELETE FROM metadata WHERE key='source_identity'")
        _install_manifest_freeze_triggers(db)


def _upload_migration(db, version):
    if version == 1:
        db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS remote_dirs(path TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,started_at TEXT,completed_at TEXT,last_error TEXT,failure_class TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS units(id INTEGER PRIMARY KEY,remote_parent TEXT NOT NULL,local_paths TEXT NOT NULL,files INTEGER NOT NULL,bytes INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,started_at TEXT,completed_at TEXT,last_error TEXT,log_path TEXT,failure_class TEXT,remediation_attempts INTEGER NOT NULL DEFAULT 0)")
    elif version == 2:
        for name, definition in {
            "attempts": "INTEGER NOT NULL DEFAULT 0", "started_at": "TEXT", "completed_at": "TEXT",
            "failure_class": "TEXT",
        }.items():
            _add_column(db, "remote_dirs", name, definition)
        _add_column(db, "units", "failure_class", "TEXT")
        _add_column(db, "units", "remediation_attempts", "INTEGER NOT NULL DEFAULT 0")
        db.execute("CREATE TABLE IF NOT EXISTS remediations(unit_id INTEGER NOT NULL,strategy TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,attempted_at TEXT,details TEXT,log_path TEXT,PRIMARY KEY(unit_id,strategy))")
    elif version == 3:
        for name, definition in {
            "status": "TEXT NOT NULL DEFAULT 'pending'", "attempts": "INTEGER NOT NULL DEFAULT 0",
            "started_at": "TEXT", "completed_at": "TEXT", "last_error": "TEXT", "failure_class": "TEXT",
        }.items():
            _add_column(db, "remote_dirs", name, definition)
        for name, definition in {
            "remote_parent": "TEXT NOT NULL DEFAULT ''", "local_paths": "TEXT NOT NULL DEFAULT '[]'",
            "files": "INTEGER NOT NULL DEFAULT 0", "bytes": "INTEGER NOT NULL DEFAULT 0",
            "status": "TEXT NOT NULL DEFAULT 'pending'", "attempts": "INTEGER NOT NULL DEFAULT 0",
            "started_at": "TEXT", "completed_at": "TEXT", "last_error": "TEXT", "log_path": "TEXT",
            "failure_class": "TEXT", "remediation_attempts": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            _add_column(db, "units", name, definition)
        db.execute("CREATE TABLE IF NOT EXISTS remediations(unit_id INTEGER NOT NULL,strategy TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,attempted_at TEXT,details TEXT,log_path TEXT,PRIMARY KEY(unit_id,strategy))")
    elif version == 4:
        pass


def _verify_migration(db, version):
    if version == 1:
        db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS directories(path TEXT PRIMARY KEY,status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,expected_files INTEGER NOT NULL DEFAULT 0,expected_entries INTEGER NOT NULL DEFAULT 0,expected_bytes INTEGER NOT NULL DEFAULT 0,mismatch_count INTEGER NOT NULL DEFAULT 0,last_error TEXT,error_class TEXT,started_at TEXT,completed_at TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS mismatches(directory_path TEXT NOT NULL,path TEXT NOT NULL,kind TEXT NOT NULL,expected TEXT,actual TEXT,PRIMARY KEY(directory_path,path,kind))")
    elif version == 2:
        for name, definition in {
            "expected_bytes": "INTEGER NOT NULL DEFAULT 0", "error_class": "TEXT", "started_at": "TEXT",
        }.items():
            _add_column(db, "directories", name, definition)
    elif version == 3:
        for name, definition in {
            "status": "TEXT NOT NULL DEFAULT 'pending'", "attempts": "INTEGER NOT NULL DEFAULT 0",
            "expected_files": "INTEGER NOT NULL DEFAULT 0", "expected_entries": "INTEGER NOT NULL DEFAULT 0",
            "expected_bytes": "INTEGER NOT NULL DEFAULT 0", "mismatch_count": "INTEGER NOT NULL DEFAULT 0",
            "last_error": "TEXT", "error_class": "TEXT", "started_at": "TEXT", "completed_at": "TEXT",
        }.items():
            _add_column(db, "directories", name, definition)
        db.execute("CREATE TABLE IF NOT EXISTS mismatches(directory_path TEXT NOT NULL,path TEXT NOT NULL,kind TEXT NOT NULL,expected TEXT,actual TEXT,PRIMARY KEY(directory_path,path,kind))")
    elif version == 4:
        pass


def migrate_database(path, component, target_version, migration):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=60, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS schema_versions(component TEXT PRIMARY KEY,version INTEGER NOT NULL,applied_at TEXT NOT NULL)")
        row = db.execute("SELECT version FROM schema_versions WHERE component=?", (component,)).fetchone()
        current = row[0] if row else 0
        if current > target_version:
            raise RuntimeError(f"{component} database schema {current} is newer than supported {target_version}")
        for version in range(current + 1, target_version + 1):
            migration(db, version)
            db.execute("INSERT OR REPLACE INTO schema_versions VALUES (?,?,?)", (component, version, now()))
        db.execute(f"PRAGMA user_version={target_version}")
        db.execute("COMMIT")
        return db
    except Exception:
        with contextlib.suppress(sqlite3.Error):
            db.execute("ROLLBACK")
        db.close()
        raise


def open_manifest(path):
    return migrate_database(path, "manifest", MANIFEST_SCHEMA_VERSION, _manifest_migration)


def open_upload(path):
    return migrate_database(path, "upload", UPLOAD_SCHEMA_VERSION, _upload_migration)


def open_verify(path):
    return migrate_database(path, "verification", VERIFY_SCHEMA_VERSION, _verify_migration)


def open_readonly(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    db = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True, timeout=5)
    db.execute("PRAGMA query_only=ON")
    return db


def schema_version(db, component):
    try:
        row = db.execute("SELECT version FROM schema_versions WHERE component=?", (component,)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)", (key, str(value)))


def metadata(db):
    return dict(db.execute("SELECT key,value FROM metadata"))


def normalize_destination(value):
    normalized = unicodedata.normalize("NFC", str(value).strip())
    return "/" if normalized and not normalized.strip("/") else normalized.rstrip("/")


def normalize_time(value):
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except ValueError:
        return unicodedata.normalize("NFC", str(value))


def _normalized_identity_fields(fields):
    normalized = {}
    for key, value in fields.items():
        if value is None or isinstance(value, (dict, list)):
            continue
        text = unicodedata.normalize("NFC", str(value).strip())
        if text:
            normalized[str(key)] = text.casefold() if "email" in str(key).lower() else text
    return normalized


def read_fingerprint_key_file(path):
    try:
        key = Path(path).read_bytes()
    except OSError as error:
        raise ClassifiedError("identity-unavailable", "account fingerprint key file cannot be read") from error
    if len(key) < 32:
        raise ClassifiedError("identity-unavailable", "account fingerprint key file must contain at least 32 bytes")
    return key


def read_expected_account_stdin(stream):
    value = stream.read()
    lines = value.splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise ClassifiedError("identity-unavailable", "--expected-account-stdin requires exactly one identity line")
    return unicodedata.normalize("NFC", lines[0].strip())


def provider_account_fingerprint(provider, fields, fingerprint_key):
    normalized = _normalized_identity_fields(fields)
    if not normalized:
        raise ClassifiedError("identity-unavailable", "provider returned no stable account identity")
    if not isinstance(fingerprint_key, bytes) or not fingerprint_key:
        raise ClassifiedError("identity-unavailable", "account fingerprint key is required")
    for preferred in ("account_id", "account_email", "username"):
        if normalized.get(preferred):
            normalized = {preferred: normalized[preferred]}
            break
    payload = {"identity": normalized, "provider": unicodedata.normalize("NFC", provider.strip().lower())}
    message = (ACCOUNT_FINGERPRINT_ALGORITHM + "\n").encode("ascii") + canonical_json(payload).encode("utf-8")
    return hmac.new(fingerprint_key, message, hashlib.sha256).hexdigest()


def verify_expected_account(provider, expected_account, fields, fingerprint_key):
    expected = unicodedata.normalize("NFC", str(expected_account or "").strip())
    if not expected:
        raise ClassifiedError("identity-unavailable", "--expected-account-stdin is required")
    normalized = _normalized_identity_fields(fields)
    if not normalized:
        raise ClassifiedError("identity-unavailable", "provider returned no stable account identity")
    folded = expected.casefold()
    if not any(expected == value or folded == value.casefold() for value in normalized.values()):
        raise ClassifiedError("account-mismatch", "authenticated provider account does not match expected stdin identity")
    return provider_account_fingerprint(provider, normalized, fingerprint_key)


def normalize_account_fingerprint(value):
    fingerprint = str(value or "").strip().lower()
    if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
        raise RuntimeError("account fingerprint must be a 64-character SHA-256 hex digest")
    return fingerprint


def source_identity_from_metadata(values):
    remote = unicodedata.normalize("NFC", values.get("source_remote", "").strip())
    if remote and ":" not in remote:
        remote += ":"
    identity = {"provider": "pcloud", "remote": remote}
    if values.get("source_account_fingerprint"):
        identity["account_fingerprint"] = normalize_account_fingerprint(values["source_account_fingerprint"])
    return identity


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def manifest_digest(db, source_identity):
    digest = hashlib.sha256()
    digest.update(b"pcloud-logical-snapshot-v1\n")
    digest.update(canonical_json(source_identity).encode("utf-8") + b"\n")
    for path, is_dir, size, mod_time, sha1 in db.execute(
        "SELECT path,is_dir,size,mod_time,sha1 FROM entries ORDER BY path"
    ):
        row = {
            "mtime": normalize_time(mod_time),
            "path": unicodedata.normalize("NFC", path.replace("\\", "/")),
            "sha1": sha1.lower() if sha1 else None,
            "size": int(size),
            "type": "directory" if is_dir else "file",
        }
        digest.update(canonical_json(row).encode("utf-8") + b"\n")
    return digest.hexdigest()


def assert_manifest_mutable(db):
    if db.execute("SELECT 1 FROM metadata WHERE key='frozen_at'").fetchone():
        raise RuntimeError("source manifest is frozen; create a new database for a new snapshot generation")


def freeze_manifest(db, generation=None):
    values = metadata(db)
    if values.get("frozen_at"):
        return load_frozen_snapshot(db, verify_digest=True)
    if values.get("status") != "complete":
        raise RuntimeError("source inventory must be complete before freeze")
    if db.execute("SELECT 1 FROM directories WHERE status!='complete' LIMIT 1").fetchone():
        raise RuntimeError("all source directory scopes must be complete before freeze")
    if db.execute("SELECT 1 FROM entries WHERE is_dir=0 AND (sha1 IS NULL OR sha1='') LIMIT 1").fetchone():
        raise RuntimeError("every source file must have SHA1 before freeze")
    identity = source_identity_from_metadata(values)
    if not identity["remote"]:
        raise RuntimeError("source identity is missing source_remote")
    if values.get("account_binding_attention"):
        raise RuntimeError("attention required: " + values["account_binding_attention"])
    if not identity.get("account_fingerprint"):
        raise RuntimeError("source account identity is missing; inventory requires --expected-account-stdin")
    chosen_generation = int(generation if generation is not None else values.get("snapshot_generation", 1))
    if chosen_generation < 1:
        raise RuntimeError("snapshot generation must be positive")
    digest = manifest_digest(db, identity)
    snapshot_id = f"pcloud-{chosen_generation}-{digest[:24]}"
    db.execute("BEGIN IMMEDIATE")
    try:
        set_meta(db, "source_identity", canonical_json(identity))
        set_meta(db, "snapshot_digest_algorithm", SNAPSHOT_DIGEST_ALGORITHM)
        set_meta(db, "snapshot_digest", digest)
        set_meta(db, "snapshot_generation", chosen_generation)
        set_meta(db, "snapshot_id", snapshot_id)
        set_meta(db, "frozen_at", now())
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return load_frozen_snapshot(db, verify_digest=False)


def load_frozen_snapshot(db, verify_digest=True):
    values = metadata(db)
    required = ("snapshot_id", "snapshot_digest", "snapshot_digest_algorithm", "snapshot_generation",
                "frozen_at", "source_identity", "source_account_fingerprint")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise RuntimeError("source manifest is not frozen; missing " + ", ".join(missing))
    if values.get("snapshot_digest_algorithm") != SNAPSHOT_DIGEST_ALGORITHM:
        raise RuntimeError("unsupported source snapshot digest algorithm")
    if values.get("account_binding_attention"):
        raise RuntimeError("attention required: " + values["account_binding_attention"])
    try:
        identity = json.loads(values["source_identity"])
    except json.JSONDecodeError as error:
        raise RuntimeError("invalid frozen source identity") from error
    if canonical_json(source_identity_from_metadata(values)) != canonical_json(identity):
        raise RuntimeError("frozen source identity metadata mismatch")
    fingerprint = identity.get("account_fingerprint")
    if not fingerprint:
        raise RuntimeError("frozen source snapshot lacks an account fingerprint; create a new inventory")
    normalize_account_fingerprint(fingerprint)
    if verify_digest:
        actual = manifest_digest(db, identity)
        if actual != values["snapshot_digest"]:
            raise RuntimeError("frozen source manifest digest mismatch")
    return {
        "snapshot_id": values["snapshot_id"],
        "snapshot_digest": values["snapshot_digest"],
        "snapshot_digest_algorithm": values["snapshot_digest_algorithm"],
        "snapshot_generation": int(values["snapshot_generation"]),
        "frozen_at": values["frozen_at"],
        "source_identity": canonical_json(identity),
        "source_account_fingerprint": fingerprint,
    }


def snapshot_for_json(snapshot):
    result = dict(snapshot)
    identity = json.loads(result["source_identity"])
    result["source_identity"] = identity
    return result


def bind_snapshot(db, snapshot, destination, destination_account_fingerprint, manifest_path=None):
    expected = dict(snapshot)
    expected["destination_root"] = normalize_destination(destination)
    expected["destination_account_fingerprint"] = normalize_account_fingerprint(destination_account_fingerprint)
    if manifest_path is not None:
        expected["manifest"] = str(Path(manifest_path).resolve())
    current = metadata(db)
    for key, value in expected.items():
        if key in current and current[key] != str(value):
            raise RuntimeError(f"state database binding mismatch for {key}: {current[key]!r} != {str(value)!r}")
    for key, value in expected.items():
        if key not in current:
            set_meta(db, key, value)
    if current.get("destination") and normalize_destination(current["destination"]) != expected["destination_root"]:
        raise RuntimeError("state database destination binding mismatch")
    set_meta(db, "destination", expected["destination_root"])


def validate_binding(db, snapshot, destination, destination_account_fingerprint):
    values = metadata(db)
    expected_destination = normalize_destination(destination)
    for key in ("snapshot_id", "snapshot_digest", "snapshot_digest_algorithm", "snapshot_generation",
                "frozen_at", "source_identity", "source_account_fingerprint"):
        if values.get(key) != str(snapshot[key]):
            raise RuntimeError(f"state database binding mismatch for {key}")
    bound_destination = values.get("destination_root", values.get("destination", ""))
    if normalize_destination(bound_destination) != expected_destination:
        raise RuntimeError("state database destination binding mismatch")
    expected_fingerprint = normalize_account_fingerprint(destination_account_fingerprint)
    if values.get("destination_account_fingerprint") != expected_fingerprint:
        raise RuntimeError("state database destination account fingerprint mismatch")


class StageSafetyError(RuntimeError):
    def __init__(self, error_class, message):
        super().__init__(message)
        self.error_class = error_class


def safe_stage_path(root, relative, require_exists=True, expected_directory=None):
    root = Path(root)
    try:
        root_stat = os.lstat(root)
    except FileNotFoundError as error:
        raise StageSafetyError("missing", "staging root does not exist") from error
    if stat.S_ISLNK(root_stat.st_mode):
        raise StageSafetyError("symlink", "attention required: staging root is a symlink")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise StageSafetyError("type", "staging root is not a directory")
    root_resolved = root.resolve(strict=True)
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise StageSafetyError("path-escape", "attention required: path escapes staging root")
    candidate = root_resolved.joinpath(relative_path)
    current = root_resolved
    for part in relative_path.parts:
        if part in ("", "."):
            continue
        current = current / part
        try:
            item_stat = os.lstat(current)
        except FileNotFoundError:
            if require_exists:
                raise StageSafetyError("missing", "staged path is missing")
            break
        if stat.S_ISLNK(item_stat.st_mode):
            raise StageSafetyError("symlink", "attention required: staged path contains a symlink")
    if require_exists:
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(root_resolved)
        except ValueError as error:
            raise StageSafetyError("path-escape", "attention required: path escapes staging root") from error
        item_stat = os.lstat(candidate)
        if expected_directory is True and not stat.S_ISDIR(item_stat.st_mode):
            raise StageSafetyError("type", "staged path is not the expected directory")
        if expected_directory is False and not stat.S_ISREG(item_stat.st_mode):
            raise StageSafetyError("type", "staged path is not the expected regular file")
    return candidate


def safe_absolute_stage_path(root, path, expected_directory=None):
    root_resolved = safe_stage_path(root, ".", require_exists=True, expected_directory=True)
    try:
        relative = Path(path).absolute().relative_to(root_resolved)
    except ValueError as error:
        raise StageSafetyError("path-escape", "attention required: upload path escapes staging root") from error
    return safe_stage_path(root_resolved, relative, require_exists=True, expected_directory=expected_directory)


def hash_file_nofollow(path, algorithm):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.new(algorithm)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise StageSafetyError("type", "staged hash input is not a regular file")
        with os.fdopen(descriptor, "rb", buffering=1024 * 1024, closefd=False) as source:
            while True:
                block = source.read(8 * 1024 * 1024)
                if not block:
                    return digest.hexdigest()
                digest.update(block)
    finally:
        os.close(descriptor)


def _is_rclone_remote(path):
    prefix = str(path).split("/", 1)[0].split("\\", 1)[0]
    return ":" in prefix


def pcloud_rclone_command(operation, rclone_config, source, local_destination=None, stage_root=None,
                          retries=None, low_level_retries=None):
    if not _is_rclone_remote(source):
        raise RuntimeError("pCloud rclone source must be a configured remote path")
    base = ["rclone", "--config", str(rclone_config)]
    if operation == "lsjson":
        return base + ["lsjson", str(source), "--max-depth", "1", "--hash"]
    if operation not in ("copy", "copyto"):
        raise RuntimeError("rclone source operation is not allowed by the read-only policy")
    if local_destination is None or stage_root is None:
        raise RuntimeError("contained local destination is required for pCloud copy")
    if _is_rclone_remote(local_destination):
        raise RuntimeError("refusing pCloud/rclone remote as a copy destination")
    root = safe_stage_path(stage_root, ".", require_exists=True, expected_directory=True)
    destination = Path(local_destination)
    try:
        relative = destination.absolute().relative_to(root)
    except ValueError as error:
        raise StageSafetyError("path-escape", "attention required: copy destination escapes staging root") from error
    safe_stage_path(root, relative, require_exists=False)
    command = base + [operation, str(source), str(destination)]
    if retries is not None:
        command += ["--retries", str(int(retries))]
    if low_level_retries is not None:
        command += ["--low-level-retries", str(int(low_level_retries))]
    return command


def validate_upload_accepted_evidence(path, snapshot, destination, destination_account_fingerprint):
    evidence_path = Path(path)
    item_stat = os.lstat(evidence_path)
    if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISREG(item_stat.st_mode):
        raise RuntimeError("upload acceptance evidence must be a regular, non-symlink file")
    try:
        with open(evidence_path, encoding="utf-8") as source:
            evidence = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("upload acceptance evidence is unreadable") from error
    if evidence.get("status", evidence.get("state")) != "complete" or evidence.get("upload_acceptance") != "accepted":
        raise RuntimeError("upload acceptance evidence is premature or incomplete")
    if evidence.get("kind") != "proton_upload" or evidence.get("account_binding_satisfied") is not True:
        raise RuntimeError("upload acceptance evidence lacks a complete account-bound upload proof")
    for complete, expected in (("units_complete", "units_expected"),
                               ("remote_dirs_complete", "remote_dirs_expected"),
                               ("files_complete", "files_expected"),
                               ("bytes_complete", "bytes_expected")):
        if evidence.get(complete) != evidence.get(expected):
            raise RuntimeError("upload acceptance evidence counters are incomplete")
    if any(evidence.get(key, 0) for key in ("units_pending", "units_running", "units_failed",
                                            "remote_dirs_pending", "remote_dirs_running", "remote_dirs_failed")):
        raise RuntimeError("upload acceptance evidence contains unfinished or failed work")
    evidence_snapshot = evidence.get("snapshot") or {}
    for key in ("snapshot_id", "snapshot_digest", "snapshot_digest_algorithm", "snapshot_generation",
                "frozen_at", "source_account_fingerprint"):
        if str(evidence_snapshot.get(key, evidence.get(key, ""))) != str(snapshot[key]):
            raise RuntimeError(f"upload acceptance evidence binding mismatch for {key}")
    if normalize_destination(evidence.get("destination", "")) != normalize_destination(destination):
        raise RuntimeError("upload acceptance evidence destination mismatch")
    destination_fingerprint = normalize_account_fingerprint(destination_account_fingerprint)
    if evidence.get("destination_account_fingerprint") != destination_fingerprint:
        raise RuntimeError("upload acceptance evidence destination account mismatch")
    binding = {
        "destination": normalize_destination(destination),
        "destination_account_fingerprint": destination_fingerprint,
        "snapshot_digest": snapshot["snapshot_digest"],
        "snapshot_digest_algorithm": snapshot["snapshot_digest_algorithm"],
        "snapshot_generation": snapshot["snapshot_generation"],
        "frozen_at": snapshot["frozen_at"],
        "snapshot_id": snapshot["snapshot_id"],
        "source_account_fingerprint": snapshot["source_account_fingerprint"],
        "upload_acceptance": "accepted",
    }
    return hashlib.sha256(canonical_json(binding).encode("utf-8")).hexdigest()


def classify_error(text):
    lowered = str(text).lower()
    if any(token in lowered for token in ("authentication", "unauthorized", "not logged", "login required", "session expired", "invalid session", "credential")):
        return "authentication"
    if any(token in lowered for token in (
            "quota exceeded", "quota limit", "storage quota", "storage limit", "not enough storage",
            "insufficient storage", "storage capacity", "out of storage", "no space left")):
        return "quota"
    if any(token in lowered for token in ("timed out", "timeout", "too many requests", "rate limit", "temporarily unavailable", "connection reset", "network is unreachable", "service unavailable")):
        return "transport"
    if any(token in lowered for token in ("node not found", "path not found", "does not exist", "no such file")):
        return "not-found"
    if any(token in lowered for token in ("already exists", "conflict", "ambiguous", "multiple entries")):
        return "ambiguity"
    if "mime type of the file is invalid" in lowered:
        return "mime"
    if "no paths matched" in lowered:
        return "literal-path"
    if any(token in lowered for token in ("permission denied", "forbidden")):
        return "permission"
    return "unknown"


class ClassifiedError(RuntimeError):
    def __init__(self, error_class, message):
        super().__init__(message)
        self.error_class = error_class
