#!/usr/bin/env python3
"""Plan and run a snapshot-bound, resumable Proton upload."""

import argparse
from collections import deque
import concurrent.futures
import datetime as dt
import glob
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from migration_common import (
    ClassifiedError, StageSafetyError, atomic_json, bind_snapshot, classify_error as common_classify_error,
    load_frozen_snapshot, metadata, normalize_destination, now, open_readonly, open_upload,
    normalize_account_fingerprint, safe_absolute_stage_path, safe_stage_path, schema_version,
    set_meta, snapshot_for_json, validate_binding,
)


def classify_failure(text):
    value = common_classify_error(text)
    return "transient" if value == "transport" else value


def connect(path):
    return open_upload(path)


def connect_readonly(path):
    return open_readonly(path)


def remote_join(root, relative):
    return root if not relative else root.rstrip("/") + "/" + relative


def parent_path(path):
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _snapshot_from_metadata(values):
    return {key: values[key] for key in (
        "snapshot_id", "snapshot_digest", "snapshot_digest_algorithm", "snapshot_generation",
        "frozen_at", "source_identity", "source_account_fingerprint",
    ) if key in values}


def summary(database):
    values = metadata(database)
    units = database.execute(
        "SELECT COUNT(*),COALESCE(SUM(status='complete'),0),COALESCE(SUM(status='pending'),0),"
        "COALESCE(SUM(status='running'),0),COALESCE(SUM(status='failed'),0),COALESCE(SUM(files),0),"
        "COALESCE(SUM(bytes),0),COALESCE(SUM(CASE WHEN status='complete' THEN files ELSE 0 END),0),"
        "COALESCE(SUM(CASE WHEN status='complete' THEN bytes ELSE 0 END),0),COALESCE(SUM(attempts),0),"
        "COALESCE(SUM(attempts>1),0),COALESCE(SUM(CASE WHEN status='running' THEN files ELSE 0 END),0),"
        "COALESCE(SUM(CASE WHEN status='running' THEN bytes ELSE 0 END),0) FROM units"
    ).fetchone()
    directories = database.execute(
        "SELECT COUNT(*),COALESCE(SUM(status='complete'),0),COALESCE(SUM(status='pending'),0),"
        "COALESCE(SUM(status='running'),0),COALESCE(SUM(status='failed'),0),COALESCE(SUM(attempts),0) FROM remote_dirs"
    ).fetchone()
    failure_classes = {}
    for table in ("units", "remote_dirs"):
        for error_class, count in database.execute(
            f"SELECT COALESCE(failure_class,'unclassified'),COUNT(*) FROM {table} WHERE status='failed' GROUP BY failure_class"
        ):
            failure_classes[error_class] = failure_classes.get(error_class, 0) + count
    remediation_count = database.execute("SELECT COUNT(*) FROM remediations").fetchone()[0]
    started = values.get("upload_started_at")
    elapsed = max(0.001, time.time() - dt.datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()) if started else 0.001
    rate = units[8] / elapsed if started else 0.0
    status = values.get("status", "planned")
    snapshot = _snapshot_from_metadata(values)
    account_binding = bool(snapshot.get("source_account_fingerprint") and values.get("destination_account_fingerprint"))
    result = {
        "kind": "proton_upload", "phase": "upload-acceptance", "state": status, "status": status,
        "schema_version": schema_version(database, "upload"), "destination": values.get("destination_root", values.get("destination")),
        "started_at": values.get("started_at"), "upload_started_at": started, "updated_at": now(),
        "last_progress_at": values.get("last_progress_at"), "completed_at": values.get("completed_at"),
        "units_expected": units[0], "units_complete": units[1], "units_pending": units[2],
        "units_running": units[3], "units_failed": units[4], "unit_attempts": units[9], "units_retried": units[10],
        "files_expected": units[5], "files_complete": units[7], "files_active": units[11],
        "bytes_expected": units[6], "bytes_complete": units[8], "bytes_active": units[12],
        "file_percent": round(100 * units[7] / units[5], 4) if units[5] else 100.0,
        "byte_percent": round(100 * units[8] / units[6], 4) if units[6] else 100.0,
        "completed_bytes_per_second": round(rate, 3),
        "eta_seconds": round((units[6] - units[8]) / rate, 1) if rate else None,
        "remote_dirs_expected": directories[0], "remote_dirs_complete": directories[1],
        "remote_dirs_pending": directories[2], "remote_dirs_running": directories[3],
        "remote_dirs_failed": directories[4], "remote_directory_attempts": directories[5],
        "failure_classes": failure_classes,
        "attempts_exhausted": failure_classes.get("max-attempts", 0),
        "auth_failures": failure_classes.get("authentication", 0),
        "quota_failures": failure_classes.get("quota", 0),
        "error_failures": units[4] + directories[4],
        "remediation_count": remediation_count,
        "attention_required": status in ("blocked-authentication", "blocked-quota", "blocked-remediation", "attention-required"),
        "upload_acceptance": "accepted" if status == "complete" and account_binding else "incomplete",
        "source_account_fingerprint": snapshot.get("source_account_fingerprint"),
        "destination_account_fingerprint": values.get("destination_account_fingerprint"),
        "account_binding_satisfied": account_binding,
        "migration_completion_gate": {"satisfied": False, "reason": "destination verification is required after upload acceptance"},
        "active_unit_progress_note": "active shard bytes are not counted because Proton exposes no reliable intra-upload byte counter",
    }
    if snapshot:
        result["snapshot"] = snapshot_for_json(snapshot)
    return result


def write_progress(database, path):
    set_meta(database, "last_progress_at", now())
    database.commit()
    value = summary(database)
    atomic_json(path, value)
    return value


def _open_frozen_manifest(path):
    manifest = open_readonly(path)
    return manifest, load_frozen_snapshot(manifest, verify_digest=True)


def create_plan(args):
    if Path(args.db).exists() and args.refresh:
        raise SystemExit("refusing destructive refresh of an upload database; use a new database path")
    manifest, snapshot = _open_frozen_manifest(args.manifest)
    destination = normalize_destination(args.destination)
    database = connect(args.db)
    existing = database.execute("SELECT COUNT(*) FROM units").fetchone()[0]
    if existing:
        if not metadata(database).get("destination_account_fingerprint"):
            raise RuntimeError("attention required: legacy upload state has no destination account fingerprint; create a new plan")
        bind_snapshot(database, snapshot, destination, args.destination_account_fingerprint, args.manifest)
        validate_binding(database, snapshot, destination, args.destination_account_fingerprint)
        database.commit()
        result = write_progress(database, args.progress)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    try:
        stage = safe_stage_path(args.stage, ".", require_exists=True, expected_directory=True)
        for path, is_dir in manifest.execute("SELECT path,is_dir FROM entries ORDER BY path"):
            safe_stage_path(stage, path, require_exists=True, expected_directory=bool(is_dir))
    except StageSafetyError as error:
        atomic_json(args.progress, {
            "kind": "proton_upload", "status": "attention-required", "attention_required": True,
            "error_class": error.error_class, "error": str(error),
            "source_account_fingerprint": snapshot.get("source_account_fingerprint"),
            "destination_account_fingerprint": normalize_account_fingerprint(args.destination_account_fingerprint),
        })
        raise SystemExit(str(error)) from error
    dirs = {"": {"children": [], "files": []}}
    for path, parent, is_dir, size in manifest.execute("SELECT path,parent_path,is_dir,size FROM entries ORDER BY path"):
        dirs.setdefault(parent, {"children": [], "files": []})
        if is_dir:
            dirs.setdefault(path, {"children": [], "files": []})
            dirs[parent]["children"].append(path)
        else:
            dirs[parent]["files"].append((path, max(0, size)))
    totals = {}
    for path in sorted(dirs, key=lambda item: (item.count("/") + 1 if item else 0), reverse=True):
        totals[path] = (
            len(dirs[path]["files"]) + sum(totals[child][0] for child in dirs[path]["children"]),
            sum(size for _, size in dirs[path]["files"]) + sum(totals[child][1] for child in dirs[path]["children"]),
        )
    remote_dirs = {destination}
    units = []

    def add_file_batches(directory):
        batch, files, byte_count = [], 0, 0
        for path, size in dirs[directory]["files"]:
            if batch and (len(batch) >= args.max_paths or files + 1 > args.max_files or byte_count + size > args.max_bytes):
                units.append((remote_join(destination, directory), batch, files, byte_count))
                batch, files, byte_count = [], 0, 0
            batch.append(str(stage / path))
            files += 1
            byte_count += size
        if batch:
            units.append((remote_join(destination, directory), batch, files, byte_count))

    def partition(directory):
        files, byte_count = totals[directory]
        if directory and files <= args.max_files and byte_count <= args.max_bytes:
            units.append((remote_join(destination, parent_path(directory)), [str(stage / directory)], files, byte_count))
            return
        remote_dirs.add(remote_join(destination, directory))
        add_file_batches(directory)
        for child in dirs[directory]["children"]:
            partition(child)

    partition("")
    database.execute("BEGIN IMMEDIATE")
    try:
        for path in sorted(remote_dirs, key=lambda item: (item.count("/"), item)):
            database.execute("INSERT INTO remote_dirs(path,status) VALUES (?,'pending')", (path,))
        for remote_parent, paths, files, byte_count in units:
            database.execute("INSERT INTO units(remote_parent,local_paths,files,bytes,status) VALUES (?,?,?,?,'pending')",
                             (remote_parent, json.dumps(paths, ensure_ascii=False), files, byte_count))
        bind_snapshot(database, snapshot, destination, args.destination_account_fingerprint, args.manifest)
        for key, value in (("status", "planned"), ("planned_at", now()), ("stage", str(stage)),
                           ("max_files", args.max_files), ("max_bytes", args.max_bytes), ("max_paths", args.max_paths)):
            set_meta(database, key, value)
        database.execute("COMMIT")
    except Exception:
        database.execute("ROLLBACK")
        raise
    result = write_progress(database, args.progress)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def proton_command(args, command, cache_dir=None, output=None):
    environment = os.environ.copy()
    if cache_dir:
        environment["PROTON_DRIVE_CACHE_DIR"] = cache_dir
    return subprocess.run([args.proton_run, args.proton_bin] + command, env=environment, stdout=output or subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=args.timeout)


def _output_text(result):
    return result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else (result.stdout or "")


def ensure_remote_dir(args, path):
    try:
        info = proton_command(args, ["filesystem", "info", "-j", path])
    except Exception as error:
        raise ClassifiedError(common_classify_error(str(error)), str(error)) from error
    text = _output_text(info)
    if info.returncode == 0:
        try:
            item = json.loads(text)
        except json.JSONDecodeError as error:
            raise ClassifiedError("ambiguity", f"unreadable successful info response for {path}: {error}") from error
        if item.get("type") not in (None, "folder"):
            raise ClassifiedError("ambiguity", f"destination path exists and is not a folder: {path}")
        return
    error_class = common_classify_error(text)
    if error_class != "not-found":
        raise ClassifiedError(error_class, text[-4000:])
    parent = path.rsplit("/", 1)[0] or "/"
    name = path.rsplit("/", 1)[-1]
    created = proton_command(args, ["filesystem", "create-folder", parent, name])
    if created.returncode:
        created_text = _output_text(created)
        raise ClassifiedError(common_classify_error(created_text), created_text[-4000:])


def _validate_upload_binding(database, destination_account_fingerprint):
    values = metadata(database)
    manifest_path = values.get("manifest")
    if not manifest_path:
        raise RuntimeError("upload database is not bound to a manifest")
    manifest, snapshot = _open_frozen_manifest(manifest_path)
    validate_binding(database, snapshot, values.get("destination_root", values.get("destination", "")),
                     destination_account_fingerprint)
    manifest.close()
    return snapshot


def run_upload(args):
    if not Path(args.db).is_file():
        raise SystemExit("upload plan does not exist")
    database = connect(args.db)
    _validate_upload_binding(database, args.destination_account_fingerprint)
    database.execute("UPDATE units SET status=CASE WHEN attempts<? THEN 'pending' ELSE 'failed' END, failure_class=CASE WHEN attempts<? THEN failure_class ELSE 'max-attempts' END WHERE status='running'", (args.max_attempts, args.max_attempts))
    database.execute("UPDATE remote_dirs SET status=CASE WHEN attempts<? THEN 'pending' ELSE 'failed' END, failure_class=CASE WHEN attempts<? THEN failure_class ELSE 'max-attempts' END WHERE status='running'", (args.max_attempts, args.max_attempts))
    if args.retry_failed:
        database.execute("UPDATE units SET status='pending',last_error=NULL WHERE status='failed' AND attempts<? AND COALESCE(failure_class,'') NOT IN ('authentication','quota','mime','ambiguity','permission','max-attempts')", (args.max_attempts,))
        database.execute("UPDATE remote_dirs SET status='pending',last_error=NULL WHERE status='failed' AND attempts<? AND COALESCE(failure_class,'') NOT IN ('authentication','quota','ambiguity','permission','max-attempts')", (args.max_attempts,))
    set_meta(database, "status", "preparing-directories")
    if not metadata(database).get("started_at"):
        set_meta(database, "started_at", now())
    write_progress(database, args.progress)

    auth_stop, quota_stop = False, False
    for path, attempts in database.execute("SELECT path,attempts FROM remote_dirs WHERE status='pending' AND attempts<? ORDER BY length(path),path", (args.max_attempts,)).fetchall():
        database.execute("UPDATE remote_dirs SET status='running',attempts=attempts+1,started_at=?,last_error=NULL WHERE path=?", (now(), path))
        database.commit()
        try:
            ensure_remote_dir(args, path)
            database.execute("UPDATE remote_dirs SET status='complete',completed_at=?,last_error=NULL,failure_class=NULL WHERE path=?", (now(), path))
        except ClassifiedError as error:
            database.execute("UPDATE remote_dirs SET status='failed',completed_at=?,last_error=?,failure_class=? WHERE path=?", (now(), str(error)[-4000:], error.error_class, path))
            auth_stop = error.error_class == "authentication"
            quota_stop = error.error_class == "quota"
        except Exception as error:
            error_class = common_classify_error(str(error))
            database.execute("UPDATE remote_dirs SET status='failed',completed_at=?,last_error=?,failure_class=? WHERE path=?", (now(), str(error)[-4000:], error_class, path))
            auth_stop = error_class == "authentication"
            quota_stop = error_class == "quota"
        write_progress(database, args.progress)
        if auth_stop or quota_stop:
            break

    if not auth_stop and not quota_stop and not database.execute("SELECT 1 FROM remote_dirs WHERE status!='complete' LIMIT 1").fetchone():
        set_meta(database, "status", "uploading")
        if not metadata(database).get("upload_started_at"):
            set_meta(database, "upload_started_at", now())
        database.commit()
        logs, cache_root = Path(args.logs), Path(args.cache)
        stage_root = metadata(database)["stage"]
        logs.mkdir(parents=True, exist_ok=True)
        cache_root.mkdir(parents=True, exist_ok=True)

        def upload_unit(row, slot):
            unit_id, remote_parent, paths = row
            log_path = logs / f"unit-{unit_id:06d}.log"
            command = ["filesystem", "upload", "--file-conflict-strategy", "skip", "--folder-conflict-strategy", "merge", "--skip-thumbnails"]
            checked_paths = [safe_absolute_stage_path(stage_root, path) for path in json.loads(paths)]
            command += [glob.escape(str(path)) for path in checked_paths] + [remote_parent]
            with open(log_path, "ab", buffering=0) as output:
                output.write(f"[{now()}] unit={unit_id} start\n".encode())
                result = proton_command(args, command, str(cache_root / f"worker-{slot}"), output)
                output.write(f"[{now()}] unit={unit_id} exit={result.returncode}\n".encode())
            return result.returncode, str(log_path)

        pending = deque(database.execute("SELECT id,remote_parent,local_paths FROM units WHERE status='pending' AND attempts<? ORDER BY bytes DESC,id", (args.max_attempts,)).fetchall())
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
            active, available, stop_scheduling = {}, list(range(args.workers)), False
            while active or (pending and not stop_scheduling):
                while available and pending and not stop_scheduling:
                    slot = available.pop(0)
                    row = pending.popleft()
                    database.execute("UPDATE units SET status='running',attempts=attempts+1,started_at=?,last_error=NULL WHERE id=?", (now(), row[0]))
                    database.commit()
                    active[executor.submit(upload_unit, row, slot)] = (row[0], slot)
                write_progress(database, args.progress)
                if not active:
                    break
                done, _ = concurrent.futures.wait(active, return_when=concurrent.futures.FIRST_COMPLETED)
                for future in done:
                    unit_id, slot = active.pop(future)
                    available.append(slot)
                    try:
                        code, log_path = future.result()
                        if code == 0:
                            state, error, error_class = "complete", None, None
                        else:
                            log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
                            state, error, error_class = "failed", f"Proton CLI exited {code}; see {log_path}", classify_failure(log_text)
                    except StageSafetyError as error_value:
                        state, error, log_path = "failed", str(error_value), None
                        error_class = "stage-safety"
                        stop_scheduling = True
                    except Exception as error_value:
                        state, error, log_path = "failed", str(error_value), None
                        error_class = classify_failure(error_value)
                    database.execute("UPDATE units SET status=?,completed_at=?,last_error=?,log_path=?,failure_class=? WHERE id=?", (state, now(), error, log_path, error_class, unit_id))
                    database.commit()
                    if error_class in ("authentication", "quota"):
                        stop_scheduling = True
                        auth_stop = error_class == "authentication"
                write_progress(database, args.progress)

    result = summary(database)
    auth = auth_stop or bool(database.execute("SELECT 1 FROM units WHERE status='failed' AND failure_class='authentication' UNION ALL SELECT 1 FROM remote_dirs WHERE status='failed' AND failure_class='authentication' LIMIT 1").fetchone())
    quota = bool(database.execute("SELECT 1 FROM units WHERE status='failed' AND failure_class='quota' UNION ALL SELECT 1 FROM remote_dirs WHERE status='failed' AND failure_class='quota' LIMIT 1").fetchone())
    all_complete = (result["units_complete"] == result["units_expected"] and
                    result["remote_dirs_complete"] == result["remote_dirs_expected"] and
                    result["account_binding_satisfied"])
    stage_safety = bool(database.execute("SELECT 1 FROM units WHERE status='failed' AND failure_class='stage-safety' LIMIT 1").fetchone())
    retryable = database.execute("SELECT 1 FROM units WHERE status='failed' AND attempts<? AND COALESCE(failure_class,'') NOT IN ('authentication','quota','mime','ambiguity','permission','max-attempts') UNION ALL SELECT 1 FROM remote_dirs WHERE status='failed' AND attempts<? AND COALESCE(failure_class,'') NOT IN ('authentication','quota','ambiguity','permission','max-attempts') LIMIT 1", (args.max_attempts, args.max_attempts)).fetchone()
    if all_complete:
        final_state = "complete"
        set_meta(database, "completed_at", now())
    elif auth:
        final_state = "blocked-authentication"
    elif quota:
        final_state = "blocked-quota"
    elif stage_safety:
        final_state = "attention-required"
    elif retryable:
        final_state = "recoverable"
    else:
        final_state = "blocked-remediation"
    set_meta(database, "status", final_state)
    result = write_progress(database, args.progress)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if final_state == "complete" else 1


def show_status(args):
    if not Path(args.db).is_file():
        print(json.dumps({"kind": "proton_upload", "status": "not_started", "db": args.db}, indent=2))
        return 1
    database = connect_readonly(args.db)
    expected_fingerprint = normalize_account_fingerprint(args.destination_account_fingerprint)
    if metadata(database).get("destination_account_fingerprint") != expected_fingerprint:
        raise RuntimeError("state database destination account fingerprint mismatch")
    result = summary(database)
    result["recent_failures"] = [
        {"scope": row[0], "id": row[1], "error_class": row[2], "error": row[3], "log": row[4]}
        for row in database.execute("SELECT 'unit',CAST(id AS TEXT),failure_class,last_error,log_path FROM units WHERE status='failed' UNION ALL SELECT 'directory',path,failure_class,last_error,NULL FROM remote_dirs WHERE status='failed' ORDER BY 1,2 LIMIT 50")
    ]
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--db", required=True); plan.add_argument("--manifest", required=True)
    plan.add_argument("--stage", required=True); plan.add_argument("--destination", required=True)
    plan.add_argument("--destination-account-fingerprint", required=True)
    plan.add_argument("--progress", required=True); plan.add_argument("--max-files", type=int, default=5000)
    plan.add_argument("--max-bytes", type=int, default=20_000_000_000); plan.add_argument("--max-paths", type=int, default=500)
    plan.add_argument("--refresh", action="store_true"); plan.set_defaults(function=create_plan)
    run = commands.add_parser("run")
    for name in ("db", "progress", "logs", "cache", "proton-run", "proton-bin"):
        run.add_argument("--" + name, required=True)
    run.add_argument("--destination-account-fingerprint", required=True)
    run.add_argument("--workers", type=int, default=14); run.add_argument("--timeout", type=int, default=86400)
    run.add_argument("--max-attempts", type=int, default=3); run.add_argument("--retry-failed", action="store_true")
    run.set_defaults(function=run_upload)
    status = commands.add_parser("status"); status.add_argument("--db", required=True)
    status.add_argument("--destination-account-fingerprint", required=True); status.set_defaults(function=show_status)
    args = parser.parse_args()
    return args.function(args)


if __name__ == "__main__":
    sys.exit(main())
