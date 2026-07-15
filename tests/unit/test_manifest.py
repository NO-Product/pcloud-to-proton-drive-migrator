import importlib.util
import argparse
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


MIGRATION = Path(__file__).parents[2] / "scripts" / "migration"
sys.path.insert(0, str(MIGRATION))
from migration_common import (
    ClassifiedError, freeze_manifest, manifest_digest, open_manifest, provider_account_fingerprint,
    read_expected_account_stdin, source_identity_from_metadata, verify_expected_account,
)

spec = importlib.util.spec_from_file_location("pcloud_manifest", MIGRATION / "pcloud-manifest.py")
manifest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manifest)

FINGERPRINT_KEY = b"unit-test-fingerprint-key"


class ManifestSnapshotTests(unittest.TestCase):
    def make_manifest(self, path, rows):
        db = open_manifest(path)
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('source_remote','pcloud:')")
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('source_account_fingerprint',?)",
                   (provider_account_fingerprint("pcloud", {"account_id": "123"}, FINGERPRINT_KEY),))
        db.execute("INSERT OR REPLACE INTO metadata VALUES ('status','complete')")
        db.execute("INSERT INTO directories(path,status) VALUES ('','complete')")
        for row in rows:
            db.execute("INSERT INTO entries(path,parent_path,name,is_dir,size,mod_time,sha1,seen_at) VALUES (?,?,?,?,?,?,?,?)", row)
        db.commit()
        return db

    def test_digest_is_independent_of_insertion_order(self):
        rows = [
            ("b", "", "b", 0, 2, "2024-01-01T00:00:00Z", "b" * 40, "now"),
            ("a", "", "a", 0, 1, "2024-01-01T00:00:00+00:00", "a" * 40, "now"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            first = self.make_manifest(Path(temporary) / "one.sqlite", rows)
            second = self.make_manifest(Path(temporary) / "two.sqlite", reversed(rows))
            identity = source_identity_from_metadata({
                "source_remote": "pcloud:",
                "source_account_fingerprint": provider_account_fingerprint(
                    "pcloud", {"account_id": "123"}, FINGERPRINT_KEY),
            })
            self.assertEqual(manifest_digest(first, identity), manifest_digest(second, identity))

    def test_freeze_is_idempotent_and_blocks_entry_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = self.make_manifest(Path(temporary) / "manifest.sqlite", [
                ("file", "", "file", 0, 1, None, "a" * 40, "now"),
            ])
            first = freeze_manifest(db, 7)
            second = freeze_manifest(db, 7)
            self.assertEqual(first, second)
            self.assertEqual(first["snapshot_generation"], 7)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE entries SET size=2 WHERE path='file'")

    def test_freeze_requires_complete_sha1_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = self.make_manifest(Path(temporary) / "manifest.sqlite", [
                ("file", "", "file", 0, 1, None, None, "now"),
            ])
            with self.assertRaisesRegex(RuntimeError, "SHA1"):
                freeze_manifest(db)

    def test_fingerprint_is_stable_and_contains_no_raw_identity(self):
        first = provider_account_fingerprint(
            "pcloud", {"account_id": 123, "account_email": "User@Example.test"}, FINGERPRINT_KEY)
        second = provider_account_fingerprint(
            "pcloud", {"account_email": "user@example.test", "account_id": "123"}, FINGERPRINT_KEY)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotIn("example", first)

    def test_wrong_expected_account_is_rejected(self):
        with self.assertRaisesRegex(ClassifiedError, "does not match"):
            verify_expected_account("pcloud", "wrong@example.test", {
                "account_id": "123", "account_email": "right@example.test",
            }, FINGERPRINT_KEY)

    def test_wrong_key_produces_a_different_fingerprint(self):
        identity = {"account_id": "123"}
        self.assertNotEqual(
            provider_account_fingerprint("pcloud", identity, FINGERPRINT_KEY),
            provider_account_fingerprint("pcloud", identity, b"wrong-key"),
        )

    def test_expected_account_stdin_requires_exactly_one_line(self):
        self.assertEqual(read_expected_account_stdin(io.StringIO("person@example.test\n")), "person@example.test")
        with self.assertRaisesRegex(ClassifiedError, "exactly one"):
            read_expected_account_stdin(io.StringIO("first@example.test\nsecond@example.test\n"))

    def test_account_status_does_not_create_or_mutate_manifest_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(
                remote="pcloud:", rclone_config="rclone.conf", timeout=10,
                expected_account="person@example.test", fingerprint_key=FINGERPRINT_KEY,
            )
            with mock.patch.object(manifest, "pcloud_auth", return_value={"token": "redacted"}), \
                    mock.patch.object(manifest, "api_call", return_value={
                        "result": 0, "userid": "123", "email": "person@example.test",
                        "usedquota": 10, "quota": 20,
                    }), redirect_stdout(io.StringIO()) as output:
                code = manifest.account_status(args)
            self.assertEqual(code, 0)
            self.assertEqual(list(Path(temporary).iterdir()), [])
            result = json.loads(output.getvalue())
            self.assertEqual(result["kind"], "pcloud_account")
            self.assertEqual(result["status"], "verified")
            self.assertIn("generated_at", result)
            self.assertEqual(result["classification"], "quota-available")
            self.assertEqual(result["quota"], {"used_bytes": 10, "quota_bytes": 20})
            self.assertNotIn("account_email", result)


if __name__ == "__main__":
    unittest.main()
