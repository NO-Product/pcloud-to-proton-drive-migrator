#!/usr/bin/env python3
"""Classify bounded upload recovery without mutating existing Proton data."""

import argparse
import json
from pathlib import Path
import sys

from migration_common import (
    atomic_json, load_frozen_snapshot, metadata, now, open_readonly, open_upload,
    set_meta, snapshot_for_json, validate_binding,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--temp", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--proton-run", required=True)
    parser.add_argument("--proton-bin", required=True)
    parser.add_argument("--destination-account-fingerprint", required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--max-remediation-attempts", type=int, default=2)
    args = parser.parse_args()

    if not Path(args.db).is_file():
        raise SystemExit("upload database does not exist")
    db = open_upload(args.db)
    values = metadata(db)
    manifest = open_readonly(args.manifest)
    snapshot = load_frozen_snapshot(manifest, verify_digest=True)
    validate_binding(db, snapshot, values.get("destination_root", values.get("destination", "")),
                     args.destination_account_fingerprint)

    retryable = []
    attention = []
    for unit_id, failure_class, attempts, remediation_attempts, error in db.execute(
        "SELECT id,COALESCE(failure_class,'unknown'),attempts,remediation_attempts,last_error FROM units WHERE status='failed' ORDER BY id"
    ):
        if failure_class in ("transient", "transport", "unknown", "literal-path") and attempts < args.max_attempts:
            db.execute("UPDATE units SET status='pending',last_error=NULL WHERE id=?", (unit_id,))
            retryable.append(unit_id)
            continue
        if failure_class not in ("authentication",) and remediation_attempts < args.max_remediation_attempts:
            db.execute("UPDATE units SET remediation_attempts=remediation_attempts+1 WHERE id=?", (unit_id,))
        detail = {"unit": unit_id, "failure_class": failure_class, "attempts": attempts, "error": error}
        attention.append(detail)
        db.execute("INSERT OR REPLACE INTO remediations(unit_id,strategy,status,attempts,attempted_at,details,log_path) VALUES (?,?,?,COALESCE((SELECT attempts FROM remediations WHERE unit_id=? AND strategy=?),0)+1,?,?,NULL)",
                   (unit_id, "non-destructive-review", "attention-required", unit_id, "non-destructive-review", now(), json.dumps(detail, sort_keys=True)))
    auth = any(item["failure_class"] == "authentication" for item in attention)
    remaining = db.execute("SELECT COUNT(*) FROM units WHERE status!='complete'").fetchone()[0]
    if remaining == 0:
        state = "complete"
    elif auth:
        state = "blocked-authentication"
    elif retryable:
        state = "recoverable"
    else:
        state = "blocked-remediation"
    set_meta(db, "status", state)
    set_meta(db, "last_progress_at", now())
    db.commit()
    result = {
        "kind": "proton_remediation", "phase": "upload-remediation", "state": state,
        "status": state, "generated_at": now(), "snapshot": snapshot_for_json(snapshot),
        "destination": values.get("destination_root", values.get("destination")),
        "source_account_fingerprint": snapshot.get("source_account_fingerprint"),
        "destination_account_fingerprint": values.get("destination_account_fingerprint"),
        "retryable_units": retryable, "attention_required": attention,
        "policy": "no delete, trash, replace, overwrite, move, or rename operations are performed",
        "migration_completion_gate": {"satisfied": False, "reason": "upload remediation is not destination verification"},
    }
    atomic_json(args.evidence, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if state in ("complete", "recoverable") else 2


if __name__ == "__main__":
    sys.exit(main())
