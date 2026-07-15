#!/usr/bin/env python3
"""Resumable read-only pCloud inventory and manifest-based local verification."""

import argparse
import concurrent.futures
import datetime as dt
import email.utils
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat as stat_module
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from migration_common import (
    ACCOUNT_FINGERPRINT_ALGORITHM, StageSafetyError, assert_manifest_mutable, atomic_json, freeze_manifest, hash_file_nofollow,
    load_frozen_snapshot, open_manifest, open_readonly, pcloud_rclone_command, safe_stage_path,
    read_expected_account_stdin, read_fingerprint_key_file, schema_version,
    set_meta as common_set_meta, snapshot_for_json, verify_expected_account,
)

def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def elapsed_seconds(started, ended=None):
    if not started:
        return 0.001
    start = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
    end = dt.datetime.fromisoformat((ended or now()).replace("Z", "+00:00"))
    return max(0.001, (end - start).total_seconds())


def connect(path):
    return open_manifest(path)


def connect_readonly(path):
    return open_readonly(path)


def set_meta(database, key, value):
    common_set_meta(database, key, value)


def source_path(remote, path):
    return remote if not path else remote.rstrip("/") + "/" + path.lstrip("/")


def list_directory_rclone(remote, path, folder_id, timeout, rclone_config):
    command = pcloud_rclone_command("lsjson", rclone_config, source_path(remote, path))
    started = time.monotonic()
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()[-4000:])
    try:
        return json.loads(result.stdout.decode("utf-8")), time.monotonic() - started
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"invalid rclone JSON: {error}") from error


def pcloud_auth(remote, rclone_config):
    remote_name = remote.split(":", 1)[0]
    result = subprocess.run(["rclone", "--config", rclone_config, "config", "dump"], check=True, stdout=subprocess.PIPE)
    config = json.loads(result.stdout.decode("utf-8"))[remote_name]
    token = json.loads(config["token"])["access_token"]
    return {"token": token, "hostname": config.get("hostname", "api.pcloud.com")}


def api_call(auth, method, parameters, timeout):
    url = "https://" + auth["hostname"] + "/" + method + "?" + urllib.parse.urlencode(parameters)
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + auth["token"]})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if payload.get("result") != 0:
        raise RuntimeError(f"pCloud API {payload.get('result')}: {payload.get('error', 'unknown error')}")
    return payload


def selected_root_metadata(auth, remote, timeout):
    suffix = remote.split(":", 1)[1].strip("/")
    parameters = {"folderid": 0} if not suffix else {"path": "/" + suffix}
    metadata = api_call(auth, "listfolder", {**parameters, "recursive": 0}, timeout)["metadata"]
    if not metadata.get("isfolder"):
        raise RuntimeError("configured pCloud source root is not a folder")
    return metadata


def bind_pcloud_account(database, account, expected_account, fingerprint_key):
    fingerprint = verify_expected_account("pcloud", expected_account, {
        "account_id": account.get("userid"), "account_email": account.get("email"),
    }, fingerprint_key)
    current = dict(database.execute("SELECT key,value FROM metadata")).get("source_account_fingerprint")
    if current and current != fingerprint:
        raise RuntimeError("source account fingerprint mismatch with existing manifest state")
    set_meta(database, "source_account_fingerprint", fingerprint)
    database.execute("DELETE FROM metadata WHERE key IN ('source_account_id','source_account_email','account_binding_attention')")
    return fingerprint


def api_time(value):
    if not value:
        return None
    return email.utils.parsedate_to_datetime(value).astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def list_directory_api(auth, path, folder_id, timeout):
    started = time.monotonic()
    payload = api_call(auth, "listfolder", {"folderid": folder_id, "recursive": 0}, timeout)
    items = []
    for item in payload["metadata"].get("contents", []):
        is_dir = bool(item.get("isfolder"))
        items.append({
            "Name": item["name"], "Size": int(item.get("size", -1)), "IsDir": is_dir,
            "ModTime": api_time(item.get("modified")), "CreatedTime": api_time(item.get("created")),
            "PcloudHash": str(item.get("hash")) if item.get("hash") is not None else None,
            "ID": item.get("id"), "MimeType": item.get("contenttype"),
            "FolderID": item.get("folderid") if is_dir else None,
        })
    return items, time.monotonic() - started


def recursive_scope(auth, path, folder_id, timeout):
    started = time.monotonic()
    payload = api_call(auth, "listfolder", {"folderid": folder_id, "recursive": 1}, timeout)
    return path, payload["metadata"], time.monotonic() - started


def insert_api_entry(database, parent, item, timestamp):
    name = item["name"]
    path = f"{parent}/{name}" if parent else name
    is_dir = bool(item.get("isfolder"))
    database.execute(
        "INSERT OR REPLACE INTO entries(path,parent_path,name,is_dir,size,mod_time,md5,sha1,object_id,mime_type,seen_at,created_time,pcloud_hash,checksum_status,checksum_error,checksum_attempts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (path, parent, name, int(is_dir), int(item.get("size", -1)), api_time(item.get("modified")),
         None, None, item.get("id"), item.get("contenttype"), timestamp, api_time(item.get("created")),
         str(item.get("hash")) if item.get("hash") is not None else None,
         None if is_dir else "pending", None, 0),
    )
    return path, is_dir


def inventory_recursive(args):
    database = connect(args.db)
    assert_manifest_mutable(database)
    if args.refresh:
        database.executescript("DELETE FROM verification;DELETE FROM entries;DELETE FROM directories;DELETE FROM metadata;")
    auth = pcloud_auth(args.remote, args.rclone_config)
    account = api_call(auth, "userinfo", {}, args.timeout)
    bind_pcloud_account(database, account, args.expected_account, args.fingerprint_key)
    for key, value in (("source_remote", args.remote), ("inventory_transport", "api-recursive"),
                       ("status", "running"), ("started_at", now()),
                       ("account_used_bytes", account.get("usedquota")),
                       ("account_quota_bytes", account.get("quota")), ("account_snapshot_at", now())):
        set_meta(database, key, value)
    root = selected_root_metadata(auth, args.remote, args.timeout)
    root_folder_id = root.get("folderid", 0)
    timestamp = now()
    scopes = []
    database.execute("INSERT OR REPLACE INTO directories(path,status,attempts,folder_id) VALUES ('','complete',1,?)", (root_folder_id,))
    for item in root.get("contents", []):
        path, is_dir = insert_api_entry(database, "", item, timestamp)
        if is_dir:
            folder_id = item.get("folderid")
            database.execute("INSERT OR REPLACE INTO directories(path,status,attempts,folder_id) VALUES (?,'pending',0,?)", (path, folder_id))
            scopes.append((path, folder_id))
    database.commit()
    atomic_json(args.progress, inventory_summary(database))

    def import_children(parent, metadata, seen):
        for child in metadata.get("contents", []):
            path, is_dir = insert_api_entry(database, parent, child, seen)
            if is_dir:
                database.execute(
                    "INSERT OR REPLACE INTO directories(path,status,attempts,completed_at,folder_id) VALUES (?,'complete',1,?,?)",
                    (path, seen, child.get("folderid")),
                )
                import_children(path, child, seen)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for path, folder_id in scopes:
            database.execute("UPDATE directories SET status='running',attempts=attempts+1,started_at=? WHERE path=?", (now(), path))
            futures[executor.submit(recursive_scope, auth, path, folder_id, args.timeout)] = path
        database.commit()
        for future in concurrent.futures.as_completed(futures):
            path = futures[future]
            try:
                _, metadata, elapsed = future.result()
                seen = now()
                import_children(path, metadata, seen)
                database.execute("UPDATE directories SET status='complete',completed_at=?,last_error=NULL WHERE path=?", (seen, path))
                set_meta(database, "last_scope_seconds", f"{elapsed:.3f}")
            except Exception as error:
                database.execute("UPDATE directories SET status='failed',completed_at=?,last_error=? WHERE path=?",
                                 (now(), str(error)[-4000:], path))
            database.commit()
            atomic_json(args.progress, inventory_summary(database))
    summary = inventory_summary(database)
    if summary["directories_failed"]:
        set_meta(database, "status", "failed")
        code = 1
    else:
        set_meta(database, "status", "complete")
        set_meta(database, "completed_at", now())
        code = 0
    database.commit()
    summary = inventory_summary(database)
    atomic_json(args.progress, summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return code


def inventory_summary(database):
    counts = database.execute(
        "SELECT COUNT(*),COALESCE(SUM(status='complete'),0),COALESCE(SUM(status='pending'),0),"
        "COALESCE(SUM(status='running'),0),COALESCE(SUM(status='failed'),0),"
        "COALESCE(SUM(attempts),0),COALESCE(SUM(attempts>1),0) FROM directories"
    ).fetchone()
    files, dirs, size, hashes, created = database.execute(
        "SELECT COALESCE(SUM(is_dir=0),0),COALESCE(SUM(is_dir=1),0),"
        "COALESCE(SUM(CASE WHEN is_dir=0 THEN size ELSE 0 END),0),"
        "COALESCE(SUM(is_dir=0 AND sha1 IS NOT NULL),0),COALESCE(SUM(created_time IS NOT NULL),0) FROM entries"
    ).fetchone()
    metadata = dict(database.execute("SELECT key,value FROM metadata"))
    complete = counts[0] > 0 and counts[1] == counts[0]
    elapsed = elapsed_seconds(metadata.get("started_at"), metadata.get("completed_at") if complete else None)
    account_used = int(metadata["account_used_bytes"]) if metadata.get("account_used_bytes") else None
    discovered_progress = round(100 * counts[1] / counts[0], 4) if counts[0] else 0
    result = {
        "kind": "pcloud_inventory",
        "status": "complete" if complete else metadata.get("status", "not_started"),
        "source_remote": metadata.get("source_remote"),
        "source_account_fingerprint": metadata.get("source_account_fingerprint"),
        "started_at": metadata.get("started_at"),
        "updated_at": now(),
        "completed_at": metadata.get("completed_at"),
        "directories_discovered": counts[0],
        "directories_complete": counts[1],
        "directories_pending": counts[2],
        "directories_running": counts[3],
        "directories_failed": counts[4],
        "directory_attempts": counts[5],
        "directories_retried": counts[6],
        "directory_completion_of_current_discovered": discovered_progress,
        "directory_denominator_is_final": complete,
        "files_indexed": files,
        "directory_entries_indexed": dirs,
        "bytes_indexed": size,
        "account_used_bytes": account_used,
        "inventory_account_byte_delta": (account_used - size) if account_used is not None else None,
        "inventory_matches_account_used_bytes": (account_used == size) if complete and account_used is not None else None,
        "files_with_sha1": hashes,
        "hashes_complete": files > 0 and hashes == files,
        "ambiguous_entries": 0,
        "unreadable_names": 0,
        "entries_with_created_time": created,
        "created_time_available": (metadata.get("inventory_transport") or "").startswith("api"),
        "inventory_transport": metadata.get("inventory_transport"),
        "elapsed_seconds": round(elapsed, 3),
        "directories_per_second": round(counts[1] / elapsed, 3),
        "files_per_second": round(files / elapsed, 3),
        "bytes_per_second": round(size / elapsed, 3),
    }
    result["schema_version"] = schema_version(database, "manifest")
    if metadata.get("snapshot_id"):
        result["snapshot"] = {
            "snapshot_id": metadata.get("snapshot_id"), "snapshot_digest": metadata.get("snapshot_digest"),
            "snapshot_digest_algorithm": metadata.get("snapshot_digest_algorithm"),
            "snapshot_generation": int(metadata["snapshot_generation"]), "frozen_at": metadata.get("frozen_at"),
            "source_account_fingerprint": metadata.get("source_account_fingerprint"),
        }
    return result


def inventory(args):
    database = connect(args.db)
    assert_manifest_mutable(database)
    if args.refresh:
        database.executescript("DELETE FROM verification;DELETE FROM entries;DELETE FROM directories;DELETE FROM metadata;")
    set_meta(database, "source_remote", args.remote)
    set_meta(database, "inventory_transport", args.transport)
    set_meta(database, "status", "running")
    if not database.execute("SELECT 1 FROM metadata WHERE key='started_at'").fetchone():
        set_meta(database, "started_at", now())
    database.execute("UPDATE directories SET status='pending' WHERE status='running'")
    database.execute("INSERT OR IGNORE INTO directories(path,status,folder_id) VALUES ('','pending',0)")
    if args.retry_failed:
        database.execute("UPDATE directories SET status='pending',last_error=NULL WHERE status='failed'")
    database.commit()
    atomic_json(args.progress, inventory_summary(database))

    provider_auth = pcloud_auth(args.remote, args.rclone_config)
    account = api_call(provider_auth, "userinfo", {}, args.timeout)
    bind_pcloud_account(database, account, args.expected_account, args.fingerprint_key)
    if args.transport == "api":
        auth = provider_auth
        set_meta(database, "account_used_bytes", account.get("usedquota"))
        set_meta(database, "account_quota_bytes", account.get("quota"))
        set_meta(database, "account_snapshot_at", now())
        root_folder_id = selected_root_metadata(auth, args.remote, args.timeout).get("folderid", 0)
        database.execute("UPDATE directories SET folder_id=? WHERE path=''", (root_folder_id,))
        database.commit()
    else:
        auth = None
    listing = list_directory_api if args.transport == "api" else list_directory_rclone
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}

        def fill_workers():
            slots = args.workers - len(futures)
            if slots <= 0:
                return
            pending = list(database.execute(
                "SELECT path,folder_id FROM directories WHERE status='pending' ORDER BY path LIMIT ?", (slots,)
            ))
            timestamp = now()
            for path, folder_id in pending:
                database.execute(
                    "UPDATE directories SET status='running',attempts=attempts+1,started_at=?,last_error=NULL WHERE path=?",
                    (timestamp, path),
                )
                first = auth if args.transport == "api" else args.remote
                arguments = (first, path, folder_id, args.timeout) if args.transport == "api" else (
                    first, path, folder_id, args.timeout, args.rclone_config
                )
                futures[executor.submit(listing, *arguments)] = path
            database.commit()

        fill_workers()
        while futures:
            completed, _ = concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in completed:
                parent = futures.pop(future)
                try:
                    items, elapsed = future.result()
                    timestamp = now()
                    for item in items:
                        name = item.get("Name", item.get("Path", ""))
                        path = f"{parent}/{name}" if parent else name
                        hashes = item.get("Hashes") or {}
                        is_dir = bool(item.get("IsDir"))
                        database.execute(
                            "INSERT OR REPLACE INTO entries(path,parent_path,name,is_dir,size,mod_time,md5,sha1,object_id,mime_type,seen_at,created_time,pcloud_hash,checksum_status,checksum_error,checksum_attempts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (path, parent, name, int(is_dir), int(item.get("Size", -1)), item.get("ModTime"),
                             hashes.get("md5"), hashes.get("sha1"), item.get("ID"), item.get("MimeType"), timestamp,
                             item.get("CreatedTime"), item.get("PcloudHash"),
                             "complete" if hashes.get("sha1") else (None if is_dir else "pending"), None, 0),
                        )
                        if is_dir:
                            database.execute("INSERT OR IGNORE INTO directories(path,status,folder_id) VALUES (?,'pending',?)", (path, item.get("FolderID")))
                    database.execute(
                        "UPDATE directories SET status='complete',completed_at=?,last_error=NULL WHERE path=?",
                        (timestamp, parent),
                    )
                    set_meta(database, "last_directory_seconds", f"{elapsed:.3f}")
                except Exception as error:
                    attempts = database.execute("SELECT attempts FROM directories WHERE path=?", (parent,)).fetchone()[0]
                    state = "pending" if attempts < args.retries else "failed"
                    database.execute(
                        "UPDATE directories SET status=?,completed_at=?,last_error=? WHERE path=?",
                        (state, now(), str(error)[-4000:], parent),
                    )
                database.commit()
                atomic_json(args.progress, inventory_summary(database))
            fill_workers()

    summary = inventory_summary(database)
    if summary["directories_failed"]:
        set_meta(database, "status", "failed")
        code = 1
    else:
        set_meta(database, "status", "complete")
        set_meta(database, "completed_at", now())
        code = 0
    database.commit()
    summary = inventory_summary(database)
    atomic_json(args.progress, summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return code


def checksum_inventory(args):
    database = connect(args.db)
    assert_manifest_mutable(database)
    summary = inventory_summary(database)
    if summary["status"] != "complete":
        raise SystemExit("tree inventory is incomplete; checksum enrichment cannot be finalized")
    auth = pcloud_auth(summary["source_remote"] or "pcloud:", args.rclone_config)
    account = api_call(auth, "userinfo", {}, args.timeout)
    bind_pcloud_account(database, account, args.expected_account, args.fingerprint_key)
    if args.retry_failed:
        database.execute("UPDATE entries SET checksum_status='pending',checksum_error=NULL WHERE is_dir=0 AND sha1 IS NULL")
        database.commit()
    total = database.execute("SELECT COUNT(*) FROM entries WHERE is_dir=0").fetchone()[0]
    complete = database.execute("SELECT COUNT(*) FROM entries WHERE is_dir=0 AND sha1 IS NOT NULL").fetchone()[0]
    progress = {"kind": "checksum_inventory", "status": "running", "started_at": now(), "updated_at": now(),
                "files_expected": total, "files_complete": complete, "files_complete_at_start": complete,
                "files_completed_this_run": 0, "files_failed": 0}

    def write_checksum_progress():
        elapsed = elapsed_seconds(progress["started_at"])
        total_processed = progress["files_complete"] + progress["files_failed"]
        run_processed = progress["files_completed_this_run"] + progress["files_failed"]
        progress["percent_complete"] = round(100 * total_processed / total, 4) if total else 100.0
        progress["files_per_second"] = round(run_processed / elapsed, 3)
        remaining = max(0, total - total_processed)
        progress["eta_seconds"] = round(remaining / progress["files_per_second"], 1) if progress["files_per_second"] else None
        atomic_json(args.progress, progress)

    write_checksum_progress()

    def fetch(row):
        path, object_id = row
        file_id = str(object_id).lstrip("f")
        last_error = None
        for attempt in range(1, args.retries + 1):
            try:
                payload = api_call(auth, "checksumfile", {"fileid": file_id}, args.timeout)
                return path, payload.get("md5"), payload.get("sha1"), attempt, None
            except Exception as error:
                last_error = str(error)
                time.sleep(min(attempt, 5))
        return path, None, None, args.retries, last_error

    while True:
        rows = list(database.execute(
            "SELECT path,object_id FROM entries WHERE is_dir=0 AND sha1 IS NULL AND COALESCE(checksum_status,'pending')!='failed' LIMIT ?",
            (args.batch_size,),
        ))
        if not rows:
            break
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            for path, md5, sha1, attempts, error in executor.map(fetch, rows):
                state = "complete" if sha1 else "failed"
                database.execute(
                    "UPDATE entries SET md5=?,sha1=?,checksum_status=?,checksum_error=?,checksum_attempts=checksum_attempts+? WHERE path=?",
                    (md5, sha1, state, error, attempts, path),
                )
                progress["files_complete"] += int(bool(sha1))
                progress["files_completed_this_run"] += int(bool(sha1))
                progress["files_failed"] += int(not sha1)
                progress["updated_at"] = now()
                if (progress["files_complete"] + progress["files_failed"]) % 100 == 0:
                    database.commit()
                    write_checksum_progress()
        database.commit()
        write_checksum_progress()
    failed = database.execute("SELECT COUNT(*) FROM entries WHERE is_dir=0 AND sha1 IS NULL").fetchone()[0]
    progress.update({"status": "complete" if failed == 0 else "failed", "completed_at": now(), "files_failed": failed})
    write_checksum_progress()
    print(json.dumps(progress, indent=2))
    return 0 if failed == 0 else 1


def show_status(args):
    if not os.path.exists(args.db):
        print(json.dumps({"status": "not_started", "db": args.db}, indent=2))
        return 1
    database = connect_readonly(args.db)
    result = inventory_summary(database)
    result["failed_scopes"] = [
        {"path": row[0], "attempts": row[1], "error": row[2]}
        for row in database.execute("SELECT path,attempts,last_error FROM directories WHERE status='failed' ORDER BY path LIMIT 100")
    ]
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def parse_time(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() if value else None


def calculate_hash(task):
    path, algorithm = task
    return hash_file_nofollow(path, algorithm)


def verify_local(args):
    database = connect(args.db)
    if inventory_summary(database)["status"] != "complete":
        raise SystemExit("inventory is incomplete; full local verification cannot be claimed")
    if args.hash != "none" and not inventory_summary(database)["hashes_complete"]:
        raise SystemExit("source checksums are incomplete; hash verification cannot be claimed")
    root = Path(args.stage)
    safe_stage_path(root, ".", require_exists=True, expected_directory=True)
    report = Path(args.report)
    report.parent.mkdir(parents=True, exist_ok=True)
    mismatch_path = report.with_suffix(".mismatches.jsonl")
    rows = database.execute("SELECT path,is_dir,size,mod_time,md5,sha1 FROM entries ORDER BY path").fetchall()
    expected_paths = {row[0] for row in rows}
    counters = {key: 0 for key in ("checked", "ok", "missing", "unexpected", "type", "size",
                                    "mtime", "sha1", "errors", "bytes_hashed",
                                    "bytes_hashed_at_start", "bytes_hashed_this_run")}
    counters["expected"] = len(rows)
    counters["bytes_expected"] = sum(max(0, row[2]) for row in rows if not row[1])
    started = now()
    phase = "metadata"
    if not args.resume:
        database.execute("DELETE FROM verification")
    database.commit()

    def write_progress(state="running"):
        value = dict(counters)
        value.update({"kind": "local_verification", "status": state, "phase": phase, "started_at": started,
                      "updated_at": now(), "hash_mode": args.hash, "stage": str(root)})
        value["percent_checked"] = round(100 * value["checked"] / value["expected"], 4) if value["expected"] else 0
        value["hash_percent"] = round(100 * value["bytes_hashed"] / value["bytes_expected"], 4) if args.hash != "none" and value["bytes_expected"] else None
        elapsed = elapsed_seconds(started)
        value["elapsed_seconds"] = round(elapsed, 3)
        value["entries_per_second"] = round(value["checked"] / elapsed, 3)
        value["hash_bytes_per_second"] = round(value["bytes_hashed_this_run"] / elapsed, 3) if args.hash != "none" else None
        remaining = max(0, value["bytes_expected"] - value["bytes_hashed"])
        value["hash_eta_seconds"] = round(remaining / value["hash_bytes_per_second"], 1) if value["hash_bytes_per_second"] else None
        atomic_json(args.progress, value)
        return value

    last_progress = 0.0
    hash_jobs = []
    with open(mismatch_path, "w", encoding="utf-8") as mismatches:
        def save(path, state, detail=None, algorithm=None, source_hash=None, local_hash=None,
                 stat=None, local_size=None, local_mtime_ns=None):
            database.execute(
                "INSERT OR REPLACE INTO verification(path,status,detail,checked_at,algorithm,source_hash,local_hash,local_size,local_mtime_ns) VALUES (?,?,?,?,?,?,?,?,?)",
                (path, state, json.dumps(detail), now(), algorithm, source_hash, local_hash,
                 stat.st_size if stat else local_size, stat.st_mtime_ns if stat else local_mtime_ns),
            )

        def record(path, state, detail=None, stat=None):
            counters[state] += 1
            mismatches.write(json.dumps({"path": path, "status": state, "detail": detail}, ensure_ascii=False) + "\n")
            save(path, state, detail, stat=stat)

        for path, is_dir, size, mod_time, md5, sha1 in rows:
            local = root / path
            issue = None
            stat = None
            try:
                local = safe_stage_path(root, path, require_exists=True, expected_directory=bool(is_dir))
                stat = os.stat(local, follow_symlinks=False)
                expected_hash = (sha1 if args.hash == "sha1" else md5) if not is_dir and args.hash != "none" else None
                algorithm = args.hash if expected_hash else "metadata"
                checkpoint = database.execute(
                    "SELECT status,algorithm,source_hash,local_size,local_mtime_ns FROM verification WHERE path=?", (path,)
                ).fetchone() if args.resume else None
                if checkpoint and checkpoint[0] == "ok" and checkpoint[1] == algorithm and checkpoint[2] == expected_hash and checkpoint[3] == stat.st_size and checkpoint[4] == stat.st_mtime_ns:
                    counters["checked"] += 1
                    counters["ok"] += 1
                    if expected_hash:
                        counters["bytes_hashed"] += max(0, size)
                        counters["bytes_hashed_at_start"] += max(0, size)
                    continue
                if not is_dir and stat.st_size != size:
                    issue = ("size", {"expected": size, "actual": stat.st_size})
                elif mod_time and abs(stat.st_mtime - parse_time(mod_time)) > args.mtime_tolerance:
                    issue = ("mtime", {"expected": mod_time, "actual_epoch": stat.st_mtime})
                elif not is_dir and args.hash != "none":
                    if expected_hash:
                        hash_jobs.append((path, str(local), expected_hash, size, stat.st_size, stat.st_mtime_ns))
                    else:
                        issue = ("errors", {"error": f"source {args.hash} unavailable"})
            except FileNotFoundError:
                issue = ("missing", None)
            except StageSafetyError as error:
                if error.error_class == "type":
                    issue = ("type", {"expected": "directory" if is_dir else "file", "actual": "other"})
                else:
                    issue = ("errors", {"error": str(error), "error_class": error.error_class, "attention_required": True})
            except OSError as error:
                issue = ("errors", {"error": str(error)})
            counters["checked"] += 1
            if issue:
                record(path, *issue, stat=stat)
            elif is_dir or args.hash == "none":
                counters["ok"] += 1
                save(path, "ok", algorithm="metadata", stat=stat)
            if time.monotonic() - last_progress > 5:
                database.commit()
                write_progress()
                last_progress = time.monotonic()

        phase = "content-hash" if hash_jobs else "extras"
        write_progress()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            for offset in range(0, len(hash_jobs), 1000):
                batch = hash_jobs[offset:offset + 1000]
                futures = {executor.submit(calculate_hash, (local, args.hash)): (path, expected_hash, size, local_size, mtime_ns)
                           for path, local, expected_hash, size, local_size, mtime_ns in batch}
                for future in concurrent.futures.as_completed(futures):
                    path, expected_hash, size, local_size, mtime_ns = futures[future]
                    try:
                        actual = future.result()
                        counters["bytes_hashed"] += max(0, size)
                        counters["bytes_hashed_this_run"] += max(0, size)
                        if actual.lower() == expected_hash.lower():
                            counters["ok"] += 1
                            save(path, "ok", algorithm=args.hash, source_hash=expected_hash, local_hash=actual,
                                 local_size=local_size, local_mtime_ns=mtime_ns)
                        else:
                            record(path, "sha1", {"algorithm": args.hash, "expected": expected_hash, "actual": actual})
                    except Exception as error:
                        record(path, "errors", {"error": str(error)})
                    if time.monotonic() - last_progress > 5:
                        database.commit()
                        write_progress()
                        last_progress = time.monotonic()
                database.commit()

        if args.detect_extras:
            phase = "extras"
            write_progress()
            for directory, dirnames, filenames in os.walk(root):
                relative_dir = os.path.relpath(directory, root)
                for name in list(dirnames) + filenames:
                    relative = name if relative_dir == "." else os.path.join(relative_dir, name)
                    try:
                        item_stat = os.lstat(os.path.join(directory, name))
                        if stat_module.S_ISLNK(item_stat.st_mode):
                            if name in dirnames:
                                dirnames.remove(name)
                            record(relative, "errors", {"error": "attention required: symlink in staging tree", "error_class": "symlink", "attention_required": True})
                            continue
                    except OSError as error:
                        record(relative, "errors", {"error": str(error)})
                        continue
                    if relative not in expected_paths:
                        record(relative, "unexpected")
        database.commit()

    verified = database.execute(
        "SELECT COALESCE(SUM(e.is_dir=0 AND v.status='ok'),0),"
        "COALESCE(SUM(e.is_dir=1 AND v.status='ok'),0),"
        "COALESCE(SUM(CASE WHEN e.is_dir=0 AND v.status='ok' THEN e.size ELSE 0 END),0),"
        "COALESCE(SUM(e.is_dir=0 AND v.status='ok' AND v.algorithm='sha1'),0) "
        "FROM entries e LEFT JOIN verification v ON v.path=e.path"
    ).fetchone()
    counters.update({
        "files_expected": sum(1 for row in rows if not row[1]),
        "files_verified": verified[0],
        "directories_expected": sum(1 for row in rows if row[1]),
        "directories_verified": verified[1],
        "bytes_verified": verified[2],
        "sha1_expected": sum(1 for row in rows if not row[1]),
        "sha1_verified": verified[3],
        "mismatch_classes": {key: counters[key] for key in ("missing", "unexpected", "type", "size", "sha1", "mtime")},
    })
    unresolved = sum(counters[key] for key in ("missing", "unexpected", "type", "size", "sha1", "mtime", "errors"))
    phase = "complete"
    final = write_progress("complete" if unresolved == 0 else "failed")
    final.update({"completed_at": now(), "unresolved": unresolved,
                  "attention_required": bool(database.execute(
                      "SELECT 1 FROM verification WHERE status='errors' AND detail LIKE '%attention required%' LIMIT 1"
                  ).fetchone())})
    atomic_json(report, final)
    atomic_json(args.progress, final)
    print(json.dumps(final, indent=2))
    return 0 if unresolved == 0 else 1


def apply_metadata(args):
    database = connect(args.db)
    if inventory_summary(database)["status"] != "complete":
        raise SystemExit("inventory is incomplete; metadata application cannot be finalized")
    root = Path(args.stage)
    safe_stage_path(root, ".", require_exists=True, expected_directory=True)
    rows = list(database.execute(
        "SELECT path,is_dir,mod_time FROM entries WHERE mod_time IS NOT NULL ORDER BY is_dir ASC,LENGTH(path) DESC"
    ))
    progress = {"kind": "local_metadata_application", "status": "running", "started_at": now(),
                "updated_at": now(), "expected": len(rows), "completed": 0, "failed": 0, "errors": []}
    atomic_json(args.progress, progress)
    for path, is_dir, mod_time in rows:
        local = root / path
        try:
            local = safe_stage_path(root, path, require_exists=True, expected_directory=bool(is_dir))
            timestamp = parse_time(mod_time)
            os.utime(local, (timestamp, timestamp), follow_symlinks=False)
            progress["completed"] += 1
        except (OSError, StageSafetyError) as error:
            progress["failed"] += 1
            if isinstance(error, StageSafetyError):
                progress["attention_required"] = True
            if len(progress["errors"]) < 100:
                progress["errors"].append({"path": path, "error": str(error)})
        if (progress["completed"] + progress["failed"]) % 1000 == 0:
            progress["updated_at"] = now()
            atomic_json(args.progress, progress)
    progress["status"] = "complete" if progress["failed"] == 0 else "failed"
    progress["completed_at"] = now()
    atomic_json(args.progress, progress)
    print(json.dumps(progress, indent=2, ensure_ascii=False))
    return 0 if progress["failed"] == 0 else 1


def reconcile_summary(args):
    database = connect(args.db)
    source = {}
    for top, files, dirs, size in database.execute(
        "SELECT CASE WHEN instr(path,'/')>0 THEN substr(path,1,instr(path,'/')-1) ELSE path END AS top,"
        "SUM(is_dir=0),SUM(is_dir=1),SUM(CASE WHEN is_dir=0 THEN size ELSE 0 END) FROM entries GROUP BY top ORDER BY top"
    ):
        source[top] = {"files": files or 0, "directories": dirs or 0, "bytes": size or 0}
    local = {}
    root = Path(args.stage)
    safe_stage_path(root, ".", require_exists=True, expected_directory=True)
    progress = {"kind": "top_level_reconciliation", "status": "running", "phase": "local-scan",
                "started_at": now(), "updated_at": now(), "local_files_scanned": 0, "local_bytes_scanned": 0}
    atomic_json(args.progress, progress)
    for directory, dirnames, filenames in os.walk(root):
        relative = os.path.relpath(directory, root)
        for name in list(dirnames):
            directory_stat = os.lstat(os.path.join(directory, name))
            if stat_module.S_ISLNK(directory_stat.st_mode):
                dirnames.remove(name)
                progress["attention_required"] = True
        if relative == ".":
            for name in dirnames:
                local.setdefault(name, {"files": 0, "directories": 0, "bytes": 0})["directories"] += 1
            top_for_files = None
        else:
            top_for_files = relative.split(os.sep, 1)[0]
            bucket = local.setdefault(top_for_files, {"files": 0, "directories": 0, "bytes": 0})
            if relative != top_for_files:
                bucket["directories"] += 1
        for name in filenames:
            top = name if top_for_files is None else top_for_files
            bucket = local.setdefault(top, {"files": 0, "directories": 0, "bytes": 0})
            bucket["files"] += 1
            try:
                item_stat = os.lstat(os.path.join(directory, name))
                if stat_module.S_ISLNK(item_stat.st_mode):
                    raise StageSafetyError("symlink", "attention required: symlink in staging tree")
                size = item_stat.st_size
                bucket["bytes"] += size
                progress["local_bytes_scanned"] += size
            except (OSError, StageSafetyError):
                progress["attention_required"] = True
            progress["local_files_scanned"] += 1
            if progress["local_files_scanned"] % 10000 == 0:
                progress["updated_at"] = now()
                atomic_json(args.progress, progress)
    rows = []
    for top in sorted(set(source) | set(local)):
        expected = source.get(top, {"files": 0, "directories": 0, "bytes": 0})
        actual = local.get(top, {"files": 0, "directories": 0, "bytes": 0})
        rows.append({"top_level": top, "source": expected, "local": actual,
                     "file_delta": expected["files"] - actual["files"],
                     "byte_delta": expected["bytes"] - actual["bytes"]})
    result = {"kind": "top_level_reconciliation",
              "status": "attention-required" if progress.get("attention_required") else inventory_summary(database)["status"],
              "attention_required": bool(progress.get("attention_required")),
              "generated_at": now(), "source_manifest": args.db, "stage": args.stage, "top_levels": rows}
    atomic_json(args.report, result)
    atomic_json(args.progress, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def completion_audit(args):
    database = connect(args.db)
    inventory_state = inventory_summary(database)
    operations = Path(args.operations)

    def current_file(operation, name):
        path = operations / operation / "current" / name
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as source:
                return json.load(source)
        except (OSError, json.JSONDecodeError):
            return None

    def operation_state(operation):
        path = operations / operation / "current" / "status"
        return path.read_text(encoding="utf-8").strip() if path.exists() else "missing"

    reconcile = current_file("reconcile", "report.json")
    metadata = current_file("metadata-apply", "progress.json")
    verification = current_file("verify-local", "report.json")
    issues = []
    warnings = []
    try:
        snapshot = snapshot_for_json(load_frozen_snapshot(database, verify_digest=True))
    except RuntimeError as error:
        snapshot = None
        issues.append(str(error))
    if inventory_state["status"] != "complete" or inventory_state["directories_failed"]:
        issues.append("source inventory is incomplete or has failed directory scopes")
    if not inventory_state["inventory_matches_account_used_bytes"]:
        warnings.append("folder-tree bytes differ from account usedquota; virtual/shared/linked scopes use different quota semantics")
    if not inventory_state["hashes_complete"]:
        issues.append("source SHA1 checksum enrichment is incomplete")
    if not reconcile or reconcile.get("status") != "complete":
        issues.append("final top-level reconciliation report is missing or partial")
    elif any(row["file_delta"] or row["byte_delta"] for row in reconcile["top_levels"]):
        issues.append("top-level source/local counts or bytes differ")
    if operation_state("metadata-apply") != "complete" or not metadata or metadata.get("failed"):
        issues.append("local metadata application is missing or failed")
    if operation_state("verify-local") != "complete" or not verification:
        issues.append("final local verification is missing or failed")
    elif verification.get("hash_mode") != "sha1" or verification.get("unresolved") != 0:
        issues.append("final local verification is not a clean SHA1 verification")
    source_freshness = current_file("source-freshness", "report.json")
    if operation_state("source-freshness") != "complete" or not source_freshness:
        issues.append("independent live pCloud check is missing or failed")
    elif source_freshness.get("status") != "fresh" or source_freshness.get("differences"):
        issues.append("live pCloud tree differs from the frozen source manifest")
    mismatch_classes = {key: int((verification or {}).get("mismatch_classes", {}).get(key, (verification or {}).get(key, 0)) or 0)
                        for key in ("missing", "unexpected", "type", "size", "sha1", "mtime")}
    result = {"kind": "completion_audit", "status": "complete" if not issues else "incomplete",
              "generated_at": now(), "issues": issues, "warnings": warnings, "inventory": inventory_state,
              "snapshot": snapshot, "mismatch_classes": mismatch_classes, **mismatch_classes,
              "files_expected": inventory_state["files_indexed"],
              "files_verified": (verification or {}).get("files_verified", 0),
              "directories_expected": inventory_state["directory_entries_indexed"],
              "directories_verified": (verification or {}).get("directories_verified", 0),
              "bytes_expected": inventory_state["bytes_indexed"],
              "bytes_verified": (verification or {}).get("bytes_verified", 0),
              "sha1_expected": inventory_state["files_indexed"],
              "sha1_verified": (verification or {}).get("sha1_verified", 0),
              "duplicate": 0, "unreadable_name": 0,
              "operation_states": {name: operation_state(name) for name in
                                   ("inventory", "checksums", "download", "reconcile", "metadata-apply", "verify-local", "source-freshness")}}
    atomic_json(args.report, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if not issues else 1


def verify_source_freshness(args):
    scan_args = argparse.Namespace(db=args.live_db, progress=args.progress, remote=args.remote,
                                   rclone_config=args.rclone_config, workers=args.workers,
                                   timeout=args.timeout, refresh=True, expected_account=args.expected_account,
                                   fingerprint_key=args.fingerprint_key)
    if inventory_recursive(scan_args) != 0:
        raise SystemExit("live source inventory failed")
    frozen = connect_readonly(args.db)
    snapshot = load_frozen_snapshot(frozen, verify_digest=True)
    live_values = dict(connect_readonly(args.live_db).execute("SELECT key,value FROM metadata"))
    if live_values.get("source_account_fingerprint") != snapshot["source_account_fingerprint"]:
        raise RuntimeError("live source account fingerprint differs from the frozen snapshot")
    frozen.execute("ATTACH DATABASE ? AS live", (args.live_db,))
    comparisons = {
        "missing_from_live": "SELECT f.path FROM entries f LEFT JOIN live.entries l ON l.path=f.path WHERE l.path IS NULL",
        "new_in_live": "SELECT l.path FROM live.entries l LEFT JOIN entries f ON f.path=l.path WHERE f.path IS NULL",
        "metadata_or_content_changed": "SELECT f.path FROM entries f JOIN live.entries l ON l.path=f.path WHERE f.is_dir!=l.is_dir OR f.size!=l.size OR COALESCE(f.mod_time,'')!=COALESCE(l.mod_time,'') OR COALESCE(f.created_time,'')!=COALESCE(l.created_time,'') OR COALESCE(f.pcloud_hash,'')!=COALESCE(l.pcloud_hash,'') OR COALESCE(f.object_id,'')!=COALESCE(l.object_id,'')",
    }
    counts = {}
    mismatch_path = Path(args.report).with_suffix(".differences.jsonl")
    with open(mismatch_path, "w", encoding="utf-8") as output:
        for state, query in comparisons.items():
            count = 0
            for (path,) in frozen.execute(query):
                count += 1
                output.write(json.dumps({"path": path, "status": state}, ensure_ascii=False) + "\n")
            counts[state] = count
    differences = sum(counts.values())
    result = {"kind": "source_freshness_verification", "status": "fresh" if differences == 0 else "stale",
              "generated_at": now(), "differences": differences, "counts": counts,
              "snapshot": snapshot_for_json(snapshot),
              "source_account_fingerprint": snapshot["source_account_fingerprint"],
              "frozen_inventory": args.db, "live_inventory": args.live_db}
    atomic_json(args.report, result)
    atomic_json(args.progress, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if differences == 0 else 1


def remediate(args):
    database = connect_readonly(args.db)
    snapshot = load_frozen_snapshot(database, verify_digest=True)
    root = Path(args.stage)
    safe_stage_path(root, ".", require_exists=True, expected_directory=True)
    auth = pcloud_auth(args.remote, args.rclone_config)
    account = api_call(auth, "userinfo", {}, args.timeout)
    fingerprint = verify_expected_account("pcloud", args.expected_account, {
        "account_id": account.get("userid"), "account_email": account.get("email"),
    }, args.fingerprint_key)
    if fingerprint != snapshot["source_account_fingerprint"]:
        raise RuntimeError("authenticated source account does not match the frozen snapshot")
    tasks = []
    with open(args.mismatches, encoding="utf-8") as source:
        for line in source:
            item = json.loads(line)
            if item["status"] == "unexpected":
                continue
            row = database.execute("SELECT is_dir,mod_time FROM entries WHERE path=?", (item["path"],)).fetchone()
            if row:
                tasks.append((item["path"], bool(row[0]), row[1]))
    progress = {"kind": "remediation", "status": "running", "started_at": now(), "updated_at": now(),
                "expected": len(tasks), "completed": 0, "failed": 0}
    atomic_json(args.progress, progress)

    directories = [(path, mod_time) for path, is_dir, mod_time in tasks if is_dir]
    files = [(path, mod_time) for path, is_dir, mod_time in tasks if not is_dir]
    for path, mod_time in directories:
        local = safe_stage_path(root, path, require_exists=False)
        local.mkdir(parents=True, exist_ok=True)
        local = safe_stage_path(root, path, require_exists=True, expected_directory=True)
        if mod_time:
            timestamp = parse_time(mod_time)
            os.utime(local, (timestamp, timestamp), follow_symlinks=False)
        progress["completed"] += 1

    def fetch(task):
        path, _ = task
        local = safe_stage_path(root, path, require_exists=False)
        local.parent.mkdir(parents=True, exist_ok=True)
        safe_stage_path(root, local.parent.relative_to(root.resolve()), require_exists=True, expected_directory=True)
        result = subprocess.run(
            pcloud_rclone_command("copyto", args.rclone_config, source_path(args.remote, path), str(local), root,
                                  args.retries, args.low_level_retries),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return path, result.returncode, result.stderr.decode("utf-8", "replace")[-4000:]

    errors = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        for path, code, error in executor.map(fetch, files):
            progress["completed"] += 1
            if code:
                progress["failed"] += 1
                errors.append({"path": path, "error": error})
            progress["updated_at"] = now()
            atomic_json(args.progress, progress)
    progress["status"] = "complete" if not errors else "failed"
    progress["completed_at"] = now()
    progress["errors"] = errors
    atomic_json(args.progress, progress)
    print(json.dumps(progress, indent=2, ensure_ascii=False))
    return 0 if not errors else 1


def account_status(args):
    auth = pcloud_auth(args.remote, args.rclone_config)
    account = api_call(auth, "userinfo", {}, args.timeout)
    fingerprint = verify_expected_account("pcloud", args.expected_account, {
        "account_id": account.get("userid"), "account_email": account.get("email"),
    }, args.fingerprint_key)
    try:
        used_bytes = int(account.get("usedquota"))
        quota_bytes = int(account.get("quota"))
    except (TypeError, ValueError):
        used_bytes = quota_bytes = None
    if used_bytes is None or quota_bytes is None or quota_bytes < 0 or used_bytes < 0:
        classification = "quota-unavailable"
    elif used_bytes >= quota_bytes:
        classification = "quota-exhausted"
    else:
        classification = "quota-available"
    result = {
        "kind": "pcloud_account",
        "status": "verified",
        "generated_at": now(),
        "source_account_fingerprint": fingerprint,
        "fingerprint_algorithm": ACCOUNT_FINGERPRINT_ALGORITHM,
        "quota": {"used_bytes": used_bytes, "quota_bytes": quota_bytes},
        "classification": classification,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if classification != "quota-unavailable" else 2


def freeze_snapshot(args):
    database = connect(args.db)
    snapshot = freeze_manifest(database, args.generation)
    result = {"kind": "pcloud_source_snapshot", "status": "frozen", "snapshot": snapshot_for_json(snapshot)}
    if args.report:
        atomic_json(args.report, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def show_snapshot(args):
    database = connect_readonly(args.db)
    snapshot = load_frozen_snapshot(database, verify_digest=True)
    print(json.dumps({"kind": "pcloud_source_snapshot", "status": "frozen",
                      "snapshot": snapshot_for_json(snapshot)}, indent=2, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    def add_account_arguments(command):
        command.add_argument("--expected-account-stdin", action="store_true", required=True)
        command.add_argument("--fingerprint-key-file", required=True)

    scan = commands.add_parser("inventory")
    scan.add_argument("--db", required=True)
    scan.add_argument("--progress", required=True)
    scan.add_argument("--remote", default="pcloud:")
    scan.add_argument("--rclone-config", required=True)
    add_account_arguments(scan)
    scan.add_argument("--workers", type=int, default=20)
    scan.add_argument("--transport", choices=("api", "rclone"), default="api")
    scan.add_argument("--timeout", type=int, default=120)
    scan.add_argument("--retries", type=int, default=4)
    scan.add_argument("--retry-failed", action="store_true")
    scan.add_argument("--refresh", action="store_true")
    scan.set_defaults(function=inventory)
    recursive = commands.add_parser("inventory-recursive")
    recursive.add_argument("--db", required=True)
    recursive.add_argument("--progress", required=True)
    recursive.add_argument("--remote", default="pcloud:")
    recursive.add_argument("--rclone-config", required=True)
    add_account_arguments(recursive)
    recursive.add_argument("--workers", type=int, default=2)
    recursive.add_argument("--timeout", type=int, default=900)
    recursive.add_argument("--refresh", action="store_true")
    recursive.set_defaults(function=inventory_recursive)
    checksums = commands.add_parser("checksums")
    checksums.add_argument("--db", required=True)
    checksums.add_argument("--progress", required=True)
    checksums.add_argument("--rclone-config", required=True)
    add_account_arguments(checksums)
    checksums.add_argument("--workers", type=int, default=50)
    checksums.add_argument("--batch-size", type=int, default=2000)
    checksums.add_argument("--timeout", type=int, default=60)
    checksums.add_argument("--retries", type=int, default=4)
    checksums.add_argument("--retry-failed", action="store_true")
    checksums.set_defaults(function=checksum_inventory)
    status = commands.add_parser("status")
    status.add_argument("--db", required=True)
    status.set_defaults(function=show_status)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--db", required=True)
    freeze.add_argument("--generation", type=int)
    freeze.add_argument("--report")
    freeze.set_defaults(function=freeze_snapshot)
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--db", required=True)
    snapshot.set_defaults(function=show_snapshot)
    verify = commands.add_parser("verify-local")
    verify.add_argument("--db", required=True)
    verify.add_argument("--stage", required=True)
    verify.add_argument("--report", required=True)
    verify.add_argument("--progress", required=True)
    verify.add_argument("--hash", choices=("none", "md5", "sha1"), default="none")
    verify.add_argument("--workers", type=int, default=8)
    verify.add_argument("--mtime-tolerance", type=float, default=1.1)
    verify.add_argument("--detect-extras", action="store_true")
    verify.add_argument("--resume", action="store_true")
    verify.set_defaults(function=verify_local)
    metadata = commands.add_parser("apply-metadata")
    metadata.add_argument("--db", required=True)
    metadata.add_argument("--stage", required=True)
    metadata.add_argument("--progress", required=True)
    metadata.set_defaults(function=apply_metadata)
    reconcile = commands.add_parser("reconcile-summary")
    reconcile.add_argument("--db", required=True)
    reconcile.add_argument("--stage", required=True)
    reconcile.add_argument("--report", required=True)
    reconcile.add_argument("--progress", required=True)
    reconcile.set_defaults(function=reconcile_summary)
    audit = commands.add_parser("completion-audit")
    audit.add_argument("--db", required=True)
    audit.add_argument("--operations", required=True)
    audit.add_argument("--report", required=True)
    audit.set_defaults(function=completion_audit)
    freshness = commands.add_parser("verify-source")
    freshness.add_argument("--db", required=True)
    freshness.add_argument("--live-db", required=True)
    freshness.add_argument("--report", required=True)
    freshness.add_argument("--progress", required=True)
    freshness.add_argument("--remote", default="pcloud:")
    freshness.add_argument("--rclone-config", required=True)
    add_account_arguments(freshness)
    freshness.add_argument("--workers", type=int, default=2)
    freshness.add_argument("--timeout", type=int, default=900)
    freshness.set_defaults(function=verify_source_freshness)
    fix = commands.add_parser("remediate")
    fix.add_argument("--db", required=True)
    fix.add_argument("--stage", required=True)
    fix.add_argument("--mismatches", required=True)
    fix.add_argument("--progress", required=True)
    fix.add_argument("--remote", default="pcloud:")
    fix.add_argument("--rclone-config", required=True)
    add_account_arguments(fix)
    fix.add_argument("--timeout", type=int, default=120)
    fix.add_argument("--workers", type=int, default=20)
    fix.add_argument("--retries", type=int, default=10)
    fix.add_argument("--low-level-retries", type=int, default=20)
    fix.set_defaults(function=remediate)
    account = commands.add_parser("account-status")
    account.add_argument("--remote", default="pcloud:")
    account.add_argument("--rclone-config", required=True)
    account.add_argument("--timeout", type=int, default=120)
    add_account_arguments(account)
    account.set_defaults(function=account_status)
    arguments = parser.parse_args()
    if getattr(arguments, "expected_account_stdin", False):
        arguments.expected_account = read_expected_account_stdin(sys.stdin)
        arguments.fingerprint_key = read_fingerprint_key_file(arguments.fingerprint_key_file)
    return arguments.function(arguments)


if __name__ == "__main__":
    sys.exit(main())
