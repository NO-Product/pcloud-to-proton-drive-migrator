import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


MIGRATION = Path(__file__).parents[2] / "scripts" / "migration"
sys.path.insert(0, str(MIGRATION))
spec = importlib.util.spec_from_file_location("proton_account", MIGRATION / "proton-account.py")
account = importlib.util.module_from_spec(spec)
spec.loader.exec_module(account)


class ProtonAccountTests(unittest.TestCase):
    def test_extracts_stable_identity_and_numeric_quota_only(self):
        identity, quota = account.account_facts({
            "user": {"email": {"ok": True, "value": "person@example.test"}, "userID": "42"},
            "quota": {"usedBytes": 10, "quotaBytes": 20},
        })
        self.assertEqual(identity, {"account_email": "person@example.test", "account_id": "42"})
        self.assertEqual(quota, {"used_bytes": 10, "quota_bytes": 20})

    def test_version_mismatch_fails_closed_before_account_info(self):
        with tempfile.TemporaryDirectory() as temporary:
            key = Path(temporary) / "fingerprint.key"
            key.write_bytes(b"u" * 32)
            version_result = mock.Mock(returncode=0, stdout=b"proton-drive 1.2.3\n")
            argv = ["proton-account.py", "--proton-run", "run", "--proton-bin", "proton-drive",
                    "--expected-account-stdin", "--fingerprint-key-file", str(key),
                    "--expected-version", "proton-drive 9.9.9"]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(sys, "stdin", io.StringIO("person@example.test\n")), \
                    mock.patch.object(account.subprocess, "run", return_value=version_result) as run, \
                    mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                code = account.main()
            self.assertEqual(code, 2)
            self.assertEqual(run.call_count, 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["proton_version"], "proton-drive 1.2.3")
            self.assertFalse(result["version_compatible"])

    def test_wrong_identity_is_rejected_after_compatible_version(self):
        args = mock.Mock(expected_account="wrong@example.test", fingerprint_key=b"unit-test-key",
                         proton_run="run", proton_bin="proton-drive", timeout=10)
        payload = {"user": {"email": {"ok": True, "value": "right@example.test"}, "userID": "42"}}
        info_result = mock.Mock(returncode=0, stdout=json.dumps(payload).encode("utf-8"))
        with mock.patch.object(account.subprocess, "run", return_value=info_result):
            with self.assertRaisesRegex(account.ClassifiedError, "does not match"):
                account.inspect_account(args, "proton-drive 1.2.3")

    def test_success_payload_uses_canonical_account_fields(self):
        args = mock.Mock(expected_account="person@example.test", fingerprint_key=b"unit-test-key",
                         proton_run="run", proton_bin="proton-drive", timeout=10)
        payload = {"user": {"email": "person@example.test", "userID": "42"},
                   "quota": {"usedBytes": 10, "quotaBytes": 20}}
        info_result = mock.Mock(returncode=0, stdout=json.dumps(payload).encode("utf-8"))
        with mock.patch.object(account.subprocess, "run", return_value=info_result):
            result = account.inspect_account(args, "proton-drive 1.2.3")
        self.assertEqual(result["proton_version"], "proton-drive 1.2.3")
        self.assertTrue(result["version_compatible"])
        self.assertEqual(result["quota"], {"quota_bytes": 20, "used_bytes": 10})
        self.assertEqual(len(result["destination_account_fingerprint"]), 64)
        self.assertNotIn("account_fingerprint", result)


if __name__ == "__main__":
    unittest.main()
