#!/usr/bin/env python3
"""Resumable, snapshot-bound Proton destination verification."""

import argparse
from collections import deque
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from migration_common import (
    atomic_json, bind_snapshot, classify_error, load_frozen_snapshot, metadata,
    normalize_account_fingerprint, normalize_destination, normalize_time, now, open_readonly,
    open_verify, schema_version, set_meta, snapshot_for_json, validate_binding,
    validate_upload_accepted_evidence,
)


def connect(path):
    return open_verify(path)


def connect_readonly(path):
    return open_readonly(path)


def value_of(item):
    return item.get("value") if isinstance(item, dict) and item.get("ok") else None


def parse_filesystem_listing(text):
    remote = json.loads(text)
    if not isinstance(remote, list):
        raise TypeError("listing is not a JSON array")
    if not all(isinstance(item, dict) for item in remote):
        raise TypeError("listing entries are not JSON objects")
    return remote


def filesystem_listing(proton_run, proton_bin, remote_path, cache, timeout):
    env = os.environ.copy()
    env["PROTON_DRIVE_CACHE_DIR"] = str(cache)
    result = subprocess.run(
        [proton_run, proton_bin, "filesystem", "list", "-j", remote_path], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
    )
    return result, result.stdout.decode("utf-8", "replace")


def probe_filesystem_interface(args):
    result, text = filesystem_listing(
        args.proton_run, args.proton_bin, normalize_destination(args.destination), args.cache, args.timeout,
    )
    payload = {
        "kind": "proton_filesystem_interface_probe",
        "status": "incompatible",
        "filesystem_interface_compatible": False,
        "interface": "filesystem list -j",
    }
    if result.returncode:
        payload["error_class"] = classify_error(text)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 2
    try:
        remote = parse_filesystem_listing(text)
    except (json.JSONDecodeError, TypeError) as error:
        payload["error_class"] = "interface-incompatible"
        payload["error"] = f"invalid JSON listing: {error}"
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 2
    payload.update({
        "status": "compatible", "filesystem_interface_compatible": True,
        "entries_observed": len(remote),
    })
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def compare_listing(directory, expected, remote):
    mismatches, remote_by_name = [], {}
    for item in remote:
        name = value_of(item.get("name"))
        if name is None:
            mismatches.append((directory, "unreadable-name", None, json.dumps(item, sort_keys=True)))
            continue
        if name in remote_by_name:
            mismatches.append((name, "duplicate", "one entry", "multiple entries"))
        remote_by_name[name] = item
    expected_names = {item[0] for item in expected}
    for name in sorted(set(remote_by_name) - expected_names):
        mismatches.append((f"{directory}/{name}" if directory else name, "unexpected", None, remote_by_name[name].get("type")))
    for name, is_dir, size, mod_time, sha1 in expected:
        item = remote_by_name.get(name)
        path = f"{directory}/{name}" if directory else name
        if item is None:
            mismatches.append((path, "missing", "folder" if is_dir else "file", None))
            continue
        expected_type = "folder" if is_dir else "file"
        if item.get("type") != expected_type:
            mismatches.append((path, "type", expected_type, item.get("type")))
            continue
        if is_dir:
            continue
        revision = value_of(item.get("activeRevision", {})) or {}
        if revision.get("claimedSize") != size:
            mismatches.append((path, "size", str(size), str(revision.get("claimedSize"))))
        actual_sha1 = (revision.get("claimedDigests") or {}).get("sha1")
        if sha1 and (actual_sha1 or "").lower() != sha1.lower():
            mismatches.append((path, "sha1", sha1, actual_sha1))
        actual_mod = revision.get("claimedModificationTime")
        if mod_time and normalize_time(actual_mod) != normalize_time(mod_time):
            mismatches.append((path, "mtime", mod_time, actual_mod))
    return mismatches


def _snapshot_from_metadata(values):
    return {key: values[key] for key in (
        "snapshot_id", "snapshot_digest", "snapshot_digest_algorithm", "snapshot_generation",
        "frozen_at", "source_identity", "source_account_fingerprint",
    ) if key in values}


def summary(db):
    values = metadata(db)
    row = db.execute(
        "SELECT COUNT(*),COALESCE(SUM(status='complete'),0),COALESCE(SUM(status='pending'),0),"
        "COALESCE(SUM(status='running'),0),COALESCE(SUM(status='failed'),0),COALESCE(SUM(status='mismatched'),0),"
        "COALESCE(SUM(expected_files),0),COALESCE(SUM(CASE WHEN status='complete' THEN expected_files ELSE 0 END),0),"
        "COALESCE(SUM(expected_bytes),0),COALESCE(SUM(CASE WHEN status='complete' THEN expected_bytes ELSE 0 END),0),"
        "COALESCE(SUM(mismatch_count),0),COALESCE(SUM(attempts),0) FROM directories"
    ).fetchone()
    error_classes = {key: count for key, count in db.execute(
        "SELECT COALESCE(error_class,'unclassified'),COUNT(*) FROM directories WHERE status='failed' GROUP BY error_class"
    )}
    mismatch_classes = {key: 0 for key in (
        "missing", "unexpected", "type", "size", "sha1", "mtime", "duplicate", "unreadable-name"
    )}
    for key, count in db.execute("SELECT kind,COUNT(*) FROM mismatches GROUP BY kind"):
        mismatch_classes[key] = count
    status = values.get("status", "planned")
    account_binding = bool(values.get("source_account_fingerprint") and values.get("destination_account_fingerprint"))
    upload_binding = bool(values.get("upload_acceptance_digest"))
    gate = (status == "verified" and row[1] == row[0] and row[7] == row[6] and row[9] == row[8]
            and row[10] == 0 and account_binding and upload_binding)
    result = {
        "kind": "proton_verification", "phase": "destination-verification", "state": status, "status": status,
        "schema_version": schema_version(db, "verification"), "destination": values.get("destination_root", values.get("destination")),
        "started_at": values.get("started_at"), "updated_at": now(), "last_progress_at": values.get("last_progress_at"),
        "completed_at": values.get("completed_at"), "directories_expected": row[0], "directories_complete": row[1],
        "directories_pending": row[2], "directories_running": row[3], "directories_failed": row[4],
        "directories_mismatched": row[5], "files_expected": row[6], "files_verified": row[7],
        "bytes_expected": row[8], "bytes_verified": row[9], "mismatches": row[10], "attempts": row[11],
        "file_percent": round(100 * row[7] / row[6], 4) if row[6] else 100.0,
        "byte_percent": round(100 * row[9] / row[8], 4) if row[8] else 100.0,
        "error_classes": error_classes, "mismatch_classes": mismatch_classes,
        "attempts_exhausted": error_classes.get("max-attempts", 0),
        "auth_failures": error_classes.get("authentication", 0),
        "quota_failures": error_classes.get("quota", 0),
        "error_failures": row[4],
        "attention_required": status in ("blocked-authentication", "blocked-quota", "failed-verification"),
        "completion_gate": {"satisfied": gate, "requirements": {
            "status_verified": status == "verified", "mismatches_zero": row[10] == 0,
            "directories_complete": row[1] == row[0], "files_verified": row[7] == row[6],
            "bytes_verified": row[9] == row[8],
            "source_account_bound": bool(values.get("source_account_fingerprint")),
            "destination_account_bound": bool(values.get("destination_account_fingerprint")),
            "upload_acceptance_bound": upload_binding,
        }},
        "source_account_fingerprint": values.get("source_account_fingerprint"),
        "destination_account_fingerprint": values.get("destination_account_fingerprint"),
        "upload_acceptance_digest": values.get("upload_acceptance_digest"),
        "proof_scope": "source SHA1 plus local verification and Proton client-claimed size, SHA1, and modification time; Proton SHA1 is not an independent server plaintext digest",
    }
    snapshot = _snapshot_from_metadata(values)
    if snapshot:
        result["snapshot"] = snapshot_for_json(snapshot)
    return result


def write_progress(db, path):
    set_meta(db, "last_progress_at", now())
    db.commit()
    result = summary(db)
    atomic_json(path, result)
    return result


def initialize(db, manifest, snapshot, destination, destination_account_fingerprint, manifest_path,
               upload_evidence_path, upload_evidence_digest):
    destination = normalize_destination(destination)
    if db.execute("SELECT 1 FROM directories LIMIT 1").fetchone():
        existing_values = metadata(db)
        if not existing_values.get("destination_account_fingerprint"):
            raise RuntimeError("attention required: legacy verification state has no destination account fingerprint; create a new plan")
        if not existing_values.get("upload_acceptance_digest"):
            raise RuntimeError("attention required: legacy verification state has no bound upload acceptance evidence; create a new plan")
        bind_snapshot(db, snapshot, destination, destination_account_fingerprint, manifest_path)
        validate_binding(db, snapshot, destination, destination_account_fingerprint)
        if metadata(db).get("upload_acceptance_digest") != upload_evidence_digest:
            raise RuntimeError("verification upload acceptance evidence binding mismatch")
        db.commit()
        return
    counts_by_parent = {
        row[0]: (row[1] or 0, row[2] or 0, row[3] or 0)
        for row in manifest.execute("SELECT parent_path,SUM(is_dir=0),COUNT(*),SUM(CASE WHEN is_dir=0 THEN size ELSE 0 END) FROM entries GROUP BY parent_path")
    }
    db.execute("BEGIN IMMEDIATE")
    try:
        rows = [("",)] + manifest.execute("SELECT path FROM entries WHERE is_dir=1 ORDER BY path").fetchall()
        for (path,) in rows:
            counts = counts_by_parent.get(path, (0, 0, 0))
            db.execute("INSERT INTO directories(path,expected_files,expected_entries,expected_bytes) VALUES (?,?,?,?)", (path, counts[0], counts[1], counts[2]))
        bind_snapshot(db, snapshot, destination, destination_account_fingerprint, manifest_path)
        set_meta(db, "upload_acceptance_digest", upload_evidence_digest)
        set_meta(db, "upload_evidence", str(Path(upload_evidence_path).resolve()))
        set_meta(db, "status", "planned"); set_meta(db, "planned_at", now())
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def _verification_inputs(args):
    manifest = open_readonly(args.manifest)
    snapshot = load_frozen_snapshot(manifest, verify_digest=True)
    evidence_digest = validate_upload_accepted_evidence(
        args.upload_evidence, snapshot, args.destination, args.destination_account_fingerprint,
    )
    return manifest, snapshot, evidence_digest


def create_plan(args):
    manifest, snapshot, evidence_digest = _verification_inputs(args)
    db = connect(args.db)
    initialize(db, manifest, snapshot, args.destination, args.destination_account_fingerprint,
               args.manifest, args.upload_evidence, evidence_digest)
    result = write_progress(db, args.progress)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def requeue_mismatched(db, max_attempts):
    db.execute("DELETE FROM mismatches WHERE directory_path IN "
               "(SELECT path FROM directories WHERE status='mismatched' AND attempts<?)", (max_attempts,))
    db.execute("UPDATE directories SET status='pending',mismatch_count=0,last_error=NULL,error_class=NULL "
               "WHERE status='mismatched' AND attempts<?", (max_attempts,))
    db.execute("UPDATE directories SET status='failed',error_class='max-attempts',"
               "last_error='maximum mismatch verification attempts exhausted' "
               "WHERE status='mismatched' AND attempts>=?", (max_attempts,))


def run_verify(args):
    manifest, snapshot, evidence_digest = _verification_inputs(args)
    db = connect(args.db)
    initialize(db, manifest, snapshot, args.destination, args.destination_account_fingerprint,
               args.manifest, args.upload_evidence, evidence_digest)
    validate_binding(db, snapshot, args.destination, args.destination_account_fingerprint)
    db.execute("UPDATE directories SET status=CASE WHEN attempts<? THEN 'pending' ELSE 'failed' END,error_class=CASE WHEN attempts<? THEN error_class ELSE 'max-attempts' END WHERE status='running'", (args.max_attempts, args.max_attempts))
    if args.retry_failed:
        db.execute("UPDATE directories SET status='pending',last_error=NULL,error_class=NULL WHERE status='failed' AND attempts<? AND COALESCE(error_class,'') NOT IN ('authentication','quota','permission','ambiguity','max-attempts')", (args.max_attempts,))
    if args.resume:
        requeue_mismatched(db, args.max_attempts)
    db.execute("UPDATE directories SET status='failed',error_class='max-attempts',last_error=COALESCE(last_error,'maximum attempts exhausted') WHERE status='pending' AND attempts>=?", (args.max_attempts,))
    set_meta(db, "status", "verifying")
    if not metadata(db).get("started_at"):
        set_meta(db, "started_at", now())
    write_progress(db, args.progress)
    cache_root = Path(args.cache); cache_root.mkdir(parents=True, exist_ok=True)
    destination = normalize_destination(args.destination)

    def verify_one(path, expected, slot):
        remote_path = destination + ("/" + path if path else "")
        result, text = filesystem_listing(
            args.proton_run, args.proton_bin, remote_path, cache_root / f"worker-{slot}", args.timeout,
        )
        if result.returncode:
            return None, classify_error(text), text[-3000:]
        try:
            remote = parse_filesystem_listing(text)
            return compare_listing(path, expected, remote), None, None
        except (json.JSONDecodeError, TypeError) as error:
            return None, "unknown", f"invalid JSON listing: {error}; {text[-1000:]}"

    pending = deque(row[0] for row in db.execute("SELECT path FROM directories WHERE status='pending' AND attempts<? ORDER BY path", (args.max_attempts,)))
    available, terminal_stop = list(range(args.workers)), False
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        active = {}
        while active or (pending and not terminal_stop):
            while available and pending and not terminal_stop:
                path = pending.popleft()
                expected = manifest.execute("SELECT name,is_dir,size,mod_time,sha1 FROM entries WHERE parent_path=? ORDER BY name", (path,)).fetchall()
                slot = available.pop(0)
                db.execute("UPDATE directories SET status='running',attempts=attempts+1,started_at=?,last_error=NULL,error_class=NULL WHERE path=?", (now(), path))
                db.commit()
                active[executor.submit(verify_one, path, expected, slot)] = (path, slot)
            if not active:
                break
            done, _ = concurrent.futures.wait(active, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                path, slot = active.pop(future); available.append(slot)
                try:
                    mismatches, error_class, error = future.result()
                except Exception as exception:
                    mismatches, error_class, error = None, classify_error(str(exception)), str(exception)
                if mismatches is not None:
                    db.execute("DELETE FROM mismatches WHERE directory_path=?", (path,))
                    for mismatch_path, kind, expected, actual in mismatches:
                        db.execute("INSERT OR REPLACE INTO mismatches VALUES (?,?,?,?,?)", (path, mismatch_path, kind, expected, actual))
                    state = "complete" if not mismatches else "mismatched"
                    db.execute("UPDATE directories SET status=?,mismatch_count=?,completed_at=?,last_error=NULL,error_class=NULL WHERE path=?", (state, len(mismatches), now(), path))
                else:
                    db.execute("UPDATE directories SET status='failed',completed_at=?,last_error=?,error_class=? WHERE path=?", (now(), str(error)[-4000:], error_class, path))
                    if error_class in ("authentication", "quota"):
                        terminal_stop = True
                db.commit()
            write_progress(db, args.progress)

    db.execute("UPDATE directories SET status='failed',error_class='max-attempts',"
               "last_error='maximum mismatch verification attempts exhausted' "
               "WHERE status='mismatched' AND attempts>=?", (args.max_attempts,))
    db.commit()
    result = summary(db)
    auth = db.execute("SELECT 1 FROM directories WHERE status='failed' AND error_class='authentication' LIMIT 1").fetchone()
    quota = db.execute("SELECT 1 FROM directories WHERE status='failed' AND error_class='quota' LIMIT 1").fetchone()
    retryable = db.execute(
        "SELECT 1 FROM directories WHERE status='failed' AND attempts<? AND COALESCE(error_class,'') NOT IN "
        "('authentication','quota','permission','ambiguity','max-attempts') UNION ALL "
        "SELECT 1 FROM directories WHERE status='mismatched' AND attempts<? LIMIT 1",
        (args.max_attempts, args.max_attempts),
    ).fetchone()
    if (result["directories_complete"] == result["directories_expected"]
            and result["files_verified"] == result["files_expected"]
            and result["bytes_verified"] == result["bytes_expected"] and result["mismatches"] == 0):
        state = "verified"; set_meta(db, "completed_at", now())
    elif auth:
        state = "blocked-authentication"
    elif quota:
        state = "blocked-quota"
    elif retryable:
        state = "recoverable"
    else:
        state = "failed-verification"
    set_meta(db, "status", state)
    result = write_progress(db, args.progress)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if state == "verified" else 2


def show_status(args):
    if not Path(args.db).is_file():
        print(json.dumps({"kind": "proton_verification", "status": "not_started", "db": args.db}, indent=2))
        return 1
    db = connect_readonly(args.db)
    expected_fingerprint = normalize_account_fingerprint(args.destination_account_fingerprint)
    if metadata(db).get("destination_account_fingerprint") != expected_fingerprint:
        raise RuntimeError("state database destination account fingerprint mismatch")
    result = summary(db)
    result["recent_errors"] = [
        {"path": row[0], "status": row[1], "error_class": row[2], "error": row[3], "attempts": row[4]}
        for row in db.execute("SELECT path,status,error_class,last_error,attempts FROM directories WHERE status IN ('failed','mismatched') ORDER BY path LIMIT 50")
    ]
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(); commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    for name in ("db", "manifest", "destination", "destination-account-fingerprint", "upload-evidence", "progress"):
        plan.add_argument("--" + name, required=True)
    plan.set_defaults(function=create_plan)
    run = commands.add_parser("run")
    for name in ("db", "manifest", "destination", "destination-account-fingerprint", "upload-evidence", "progress", "cache", "proton-run", "proton-bin"):
        run.add_argument("--" + name, required=True)
    run.add_argument("--workers", type=int, default=20); run.add_argument("--timeout", type=int, default=1800)
    run.add_argument("--max-attempts", type=int, default=3); run.add_argument("--retry-failed", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.set_defaults(function=run_verify)
    status = commands.add_parser("status"); status.add_argument("--db", required=True)
    status.add_argument("--destination-account-fingerprint", required=True); status.set_defaults(function=show_status)
    probe = commands.add_parser("probe-filesystem-interface")
    for name in ("destination", "cache", "proton-run", "proton-bin"):
        probe.add_argument("--" + name, required=True)
    probe.add_argument("--timeout", type=int, default=1800)
    probe.set_defaults(function=probe_filesystem_interface)
    args = parser.parse_args(); return args.function(args)


if __name__ == "__main__":
    sys.exit(main())
