import argparse
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
import json
from unittest import mock

MIGRATION = Path(__file__).parents[2] / "scripts" / "migration"
sys.path.insert(0, str(MIGRATION))

from migration_common import validate_upload_accepted_evidence

spec = importlib.util.spec_from_file_location("proton_verify", MIGRATION / "proton-verify.py")
verify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify)


class VerifyStateTests(unittest.TestCase):
    def test_filesystem_probe_uses_verifier_read_only_contract(self):
        result = mock.Mock(returncode=0, stdout=b"[]")
        args = argparse.Namespace(proton_run="runner", proton_bin="proton", destination="/root",
                                  cache="/cache", timeout=10)
        with mock.patch.object(verify.subprocess, "run", return_value=result) as run, \
                redirect_stdout(io.StringIO()) as output:
            code = verify.probe_filesystem_interface(args)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["filesystem_interface_compatible"])
        self.assertEqual(run.call_args.args[0], ["runner", "proton", "filesystem", "list", "-j", "/root"])

    def test_filesystem_probe_rejects_non_array_json(self):
        result = mock.Mock(returncode=0, stdout=b"{}")
        args = argparse.Namespace(proton_run="runner", proton_bin="proton", destination="/root",
                                  cache="/cache", timeout=10)
        with mock.patch.object(verify.subprocess, "run", return_value=result), \
                redirect_stdout(io.StringIO()) as output:
            code = verify.probe_filesystem_interface(args)
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(output.getvalue())["filesystem_interface_compatible"])

    def test_status_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.sqlite"
            with redirect_stdout(io.StringIO()):
                code = verify.show_status(argparse.Namespace(
                    db=str(path), destination_account_fingerprint="b" * 64))
            self.assertEqual(code, 1)
            self.assertFalse(path.exists())

    def test_successful_listing_detects_missing_file(self):
        mismatches = verify.compare_listing("folder", [("file", 0, 3, None, "a" * 40)], [])
        self.assertEqual(mismatches, [("folder/file", "missing", "file", None)])

    def test_listing_uses_canonical_unexpected_and_mtime_keys(self):
        remote = [{"name": {"ok": True, "value": "extra"}, "type": "file"}]
        self.assertEqual(verify.compare_listing("", [], remote)[0][1], "unexpected")
        expected = [("file", 0, 3, "2024-01-01T00:00:00Z", "a" * 40)]
        remote = [{"name": {"ok": True, "value": "file"}, "type": "file",
                   "activeRevision": {"ok": True, "value": {"claimedSize": 3,
                    "claimedDigests": {"sha1": "a" * 40},
                    "claimedModificationTime": "2024-01-02T00:00:00Z"}}}]
        self.assertEqual(verify.compare_listing("", expected, remote)[0][1], "mtime")

    def test_verified_gate_requires_all_counts_and_zero_mismatches(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = verify.connect(Path(temporary) / "verify.sqlite")
            verify.set_meta(db, "status", "verified")
            verify.set_meta(db, "source_account_fingerprint", "a" * 64)
            verify.set_meta(db, "source_identity", '{"account_fingerprint":"' + "a" * 64 + '","provider":"pcloud","remote":"pcloud:"}')
            verify.set_meta(db, "destination_account_fingerprint", "b" * 64)
            verify.set_meta(db, "upload_acceptance_digest", "c" * 64)
            db.execute("INSERT INTO directories(path,status,expected_files,expected_bytes) VALUES ('','complete',1,3)")
            db.commit()
            result = verify.summary(db)
            self.assertTrue(result["completion_gate"]["satisfied"])

    def test_premature_upload_evidence_is_rejected(self):
        fingerprint = "a" * 64
        snapshot = {"snapshot_id": "snapshot", "snapshot_digest": "digest",
                    "snapshot_digest_algorithm": "sha256-manifest-v1", "snapshot_generation": 1,
                    "frozen_at": "now", "source_account_fingerprint": "b" * 64}
        with tempfile.TemporaryDirectory() as temporary:
            evidence = Path(temporary) / "upload.json"
            evidence.write_text(json.dumps({"status": "uploading", "upload_acceptance": "incomplete"}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "premature"):
                validate_upload_accepted_evidence(evidence, snapshot, "/destination", fingerprint)

    def test_resume_requeues_mismatch_only_with_attempts_remaining(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = verify.connect(Path(temporary) / "verify.sqlite")
            db.execute("INSERT INTO directories(path,status,attempts,mismatch_count) VALUES ('retry','mismatched',1,1)")
            db.execute("INSERT INTO directories(path,status,attempts,mismatch_count) VALUES ('exhausted','mismatched',3,1)")
            verify.requeue_mismatched(db, 3)
            states = dict(db.execute("SELECT path,status FROM directories"))
            self.assertEqual(states, {"retry": "pending", "exhausted": "failed"})


if __name__ == "__main__":
    unittest.main()
