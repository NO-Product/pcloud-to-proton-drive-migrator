from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts/migration/install-autonomous-runner.sh"
PREFLIGHT = ROOT / "scripts/migration/vps-preflight.sh"
SYSTEMD = ROOT / "deploy/systemd"

class InstallBundleContract(unittest.TestCase):
    def test_runtime_closure_is_explicit(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        for path in ("bin/pcloud-proton-migrate", "config/migration.env.example",
                     "config/migration.env.schema", "scripts/migration/proton-account.py",
                     "scripts/migration/migration_common.py", "scripts/migration/run-pcloud-manifest-account-bound.sh",
                     "scripts/migration/run-proton-account-bound.sh",
                     "scripts/migration/vps-preflight.sh", "scripts/migration/proton-progress-snapshot.sh",
                     "deploy/systemd/pcloud-migration-supervisor.service",
                     "deploy/systemd/proton-progress-monitor.service", "scripts/migration",
                     "scripts/lib/config.sh", "scripts/lib/runtime.sh",
                     "scripts/proton-drive/run.sh", "BUNDLE-SHA256SUMS"):
            self.assertIn(path, text)

    def test_install_is_versioned_atomic_and_opt_in(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('releases/$VERSION', text)
        self.assertIn("mv -Tf", text)
        self.assertIn("systemctl daemon-reload", text)
        self.assertNotIn("enable --now", text)
        self.assertRegex(text, r'if \[ "\$ENABLE" -eq 1 \]')
        self.assertRegex(text, r'if \[ "\$START" -eq 1 \]')
        self.assertIn("--repair", text)
        self.assertIn("sha256sum -c", text)
        self.assertIn("rollback", text)
        self.assertIn("PROJECT_VERSION", text)
        self.assertIn("does not authenticate release provenance", text)
        self.assertLess(text.index('mv -Tf -- "$next_link" "$INSTALL_ROOT/current"'),
                        text.index('"$release/deploy/systemd/$unit"'))
        self.assertLess(text.index("preflight --offline"), text.index('if [ "$START" -eq 1 ]'))
        self.assertIn('cp -a -- "$launcher" "$rollback_root/launcher"', text)
        self.assertIn(': > "$rollback_root/launcher.absent"', text)
        self.assertIn('rm -f -- "$launcher"', text)
        self.assertLess(text.index('cp -a -- "$launcher" "$rollback_root/launcher"'),
                        text.index("transaction_started=1"))

    def test_config_is_preserved_and_runtime_user_is_rendered(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn('if [ ! -e "$CONFIG_DIR/migration.env" ]', text)
        self.assertIn("PCM_RUNTIME_USER", text)
        self.assertIn("@PCM_CONFIG_FILE@", text)
        self.assertIn('s/@PCM_RUNTIME_USER@/$PCM_RUNTIME_USER/g', text)

    def test_supervisor_is_foreground_locked_and_preflight_is_durable(self) -> None:
        cli = (ROOT / "bin/pcloud-proton-migrate").read_text(encoding="utf-8")
        controller = (ROOT / "scripts/migration/full-sync.sh").read_text(encoding="utf-8")
        runtime = (ROOT / "scripts/lib/runtime.sh").read_text(encoding="utf-8")
        self.assertNotIn("setsid", runtime)
        self.assertNotIn("disown", runtime)
        self.assertIn("supervisor.lock", controller)
        self.assertIn("flock -n 5", controller)
        self.assertIn("supervisor_preflight", controller)
        self.assertIn("process.json", runtime)
        self.assertIn("proc_start_time", runtime)
        self.assertIn("boot_id", runtime)
        self.assertIn("executable", runtime)

    def test_public_recovery_and_upload_acceptance_contract(self) -> None:
        cli = (ROOT / "bin/pcloud-proton-migrate").read_text(encoding="utf-8")
        controller = (ROOT / "scripts/migration/full-sync.sh").read_text(encoding="utf-8")
        for command in ("source freshness", "local metadata", "local reconcile", "local remediate"):
            self.assertIn(command, controller)
        self.assertIn("--upload-evidence", controller)
        self.assertIn("--destination-account-fingerprint", controller)
        self.assertNotIn("--upload-accepted-evidence", controller)
        self.assertNotIn("--destination-fingerprint", controller)
        self.assertIn("final-handoff.json", controller)
        self.assertNotIn("remediate-start", controller[controller.index("recover() {"):controller.index('case "${1:-}" in')])
        self.assertIn("source freshness", cli)
        self.assertIn("source account status", cli)
        self.assertIn("compatibility probe", cli)

    def test_account_assertions_use_stdin_and_keyed_fingerprints(self) -> None:
        source = (ROOT / "scripts/migration/run-pcloud-manifest-account-bound.sh").read_text(encoding="utf-8")
        destination = (ROOT / "scripts/migration/run-proton-account-bound.sh").read_text(encoding="utf-8")
        for text in (source, destination):
            self.assertIn("--expected-account-stdin", text)
            self.assertIn("--fingerprint-key-file", text)
            self.assertNotIn("--expected-account \"", text)
        self.assertIn("--expected-version", destination)

    def test_systemd_has_one_canonical_location(self) -> None:
        expected = {"pcloud-migration-supervisor.service", "pcloud-migration-supervisor.timer",
                    "proton-progress-monitor.service", "proton-progress-monitor.timer"}
        self.assertEqual(expected, {path.name for path in SYSTEMD.iterdir()})
        self.assertFalse((ROOT / "scripts/migration/systemd").exists())

    def test_units_use_config_public_cli_and_non_root_template(self) -> None:
        for path in SYSTEMD.glob("*.service"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("--config @PCM_CONFIG_FILE@", text)
            self.assertIn("User=@PCM_RUNTIME_USER@", text)
            self.assertIn("/usr/local/bin/pcloud-proton-migrate", text)
            self.assertNotIn("/mnt/", text)
            self.assertNotIn("ExecStartPre=", text)
        supervisor = (SYSTEMD / "pcloud-migration-supervisor.service").read_text(encoding="utf-8")
        self.assertIn("TimeoutStartSec=infinity", supervisor)
        for path in SYSTEMD.glob("*.timer"):
            self.assertIn("Persistent=true", path.read_text(encoding="utf-8"))

    def test_preflight_uses_only_canonical_pcm_contract(self) -> None:
        text = PREFLIGHT.read_text(encoding="utf-8")
        required = ("PCM_BASE_DIR", "PCM_STAGING_DIR", "PCM_SOURCE_REMOTE",
                    "PCM_SOURCE_ROOT", "PCM_EXPECTED_PCLOUD_ACCOUNT", "PCM_ACCOUNT_FINGERPRINT_KEY_FILE", "PCM_DESTINATION_ROOT",
                    "PCM_EXPECTED_PROTON_ACCOUNT", "PCM_PROTON_CLI_EXPECTED_VERSION", "PCM_DESTINATION_CAPACITY_ACKNOWLEDGED", "PCM_RCLONE_CONFIG",
                    "PCM_PROTON_RUNNER", "PCM_RUNTIME_USER", "PCM_MIN_FREE_BYTES",
                    "PCM_MIN_FREE_INODES", "PCM_UPLOAD_WORKERS", "PCM_RCLONE_RETRIES",
                    "PCM_MAX_PHASE_ATTEMPTS")
        for variable in required:
            self.assertIn(variable, text)
        for legacy in ("MIGRATION_ROOT", "MIGRATION_MOUNT", "PROTON_DRIVE_BIN", "PROTON_SESSION_HOME"):
            self.assertNotIn(legacy, text)
        self.assertIn("must contain at least 32 bytes", text)

    def test_compatibility_and_completion_evidence_contracts(self) -> None:
        controller = (ROOT / "scripts/migration/full-sync.sh").read_text(encoding="utf-8")
        self.assertIn("probe-filesystem-interface", controller)
        self.assertIn("filesystem_interface_compatible", controller)
        self.assertIn("write_completion_gate_failure", controller)
        self.assertLess(controller.index("write_completion_gate_failure frozen_manifest"),
                        controller.index("completion gate frozen-manifest precondition failed"))

if __name__ == "__main__":
    unittest.main()
