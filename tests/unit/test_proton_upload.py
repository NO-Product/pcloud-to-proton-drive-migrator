import argparse
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest

MIGRATION = Path(__file__).parents[2] / "scripts" / "migration"
sys.path.insert(0, str(MIGRATION))

from migration_common import (
    ClassifiedError, StageSafetyError, bind_snapshot, pcloud_rclone_command, read_fingerprint_key_file,
    safe_stage_path, validate_binding,
)


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, MIGRATION / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


upload = load("proton_upload", "proton-upload.py")


class UploadStateTests(unittest.TestCase):
    def test_status_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "missing.sqlite"
            with redirect_stdout(io.StringIO()):
                code = upload.show_status(argparse.Namespace(
                    db=str(path), destination_account_fingerprint="b" * 64))
            self.assertEqual(code, 1)
            self.assertFalse(path.exists())

    def test_auth_classification_precedes_generic_session_text(self):
        self.assertEqual(upload.classify_failure("session expired: unauthorized"), "authentication")

    def test_quota_classification_is_explicit(self):
        self.assertEqual(upload.classify_failure("insufficient storage: quota exceeded"), "quota")

    def test_short_fingerprint_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "fingerprint.key"
            key.write_bytes(b"x" * 31)
            with self.assertRaisesRegex(ClassifiedError, "at least 32 bytes"):
                read_fingerprint_key_file(key)

    def test_upload_summary_never_claims_migration_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = upload.connect(Path(temporary) / "upload.sqlite")
            upload.set_meta(db, "status", "complete")
            upload.set_meta(db, "source_account_fingerprint", "a" * 64)
            upload.set_meta(db, "source_identity", '{"account_fingerprint":"' + "a" * 64 + '","provider":"pcloud","remote":"pcloud:"}')
            upload.set_meta(db, "destination_account_fingerprint", "b" * 64)
            db.commit()
            result = upload.summary(db)
            self.assertEqual(result["upload_acceptance"], "accepted")
            self.assertFalse(result["migration_completion_gate"]["satisfied"])

    def test_symlink_escape_is_attention_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "stage"
            outside = Path(temporary) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "escape").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(StageSafetyError, "attention required"):
                safe_stage_path(root, "escape/file", require_exists=False)

    def test_rclone_builder_rejects_remote_destination(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            stage.mkdir()
            with self.assertRaisesRegex(RuntimeError, "remote as a copy destination"):
                pcloud_rclone_command("copyto", "rclone.conf", "pcloud:file", "pcloud:other", stage)

    def test_destination_account_binding_rejects_wrong_account(self):
        snapshot = {
            "snapshot_id": "snapshot", "snapshot_digest": "digest",
            "snapshot_digest_algorithm": "sha256-manifest-v1", "snapshot_generation": 1,
            "frozen_at": "now", "source_account_fingerprint": "a" * 64,
            "source_identity": '{"account_fingerprint":"' + "a" * 64 + '","provider":"pcloud","remote":"pcloud:"}',
        }
        with tempfile.TemporaryDirectory() as temporary:
            db = upload.connect(Path(temporary) / "upload.sqlite")
            bind_snapshot(db, snapshot, "/destination", "b" * 64)
            with self.assertRaisesRegex(RuntimeError, "destination account"):
                validate_binding(db, snapshot, "/destination", "c" * 64)


if __name__ == "__main__":
    unittest.main()
