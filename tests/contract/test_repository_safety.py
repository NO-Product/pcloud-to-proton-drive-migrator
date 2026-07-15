from __future__ import annotations

import ast
import ipaddress
import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
PRIVACY_FIXTURE_EXEMPTIONS = {
    pathlib.Path("tests/unit/test_manifest.py"): {"wrong@example.test"},
    pathlib.Path("tests/unit/test_proton_account.py"): {"person@example.test"},
}
DESTRUCTIVE_FIXTURE_PREFIXES = {
    pathlib.Path("tests/contract/test_repository_safety.py"): (
        'r"(?:\\bproton-drive',
        'r"\\brclone\\s+',
    ),
}


def tracked_paths():
    result = subprocess.run(
        ("git", "ls-files", "-z"),
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = pathlib.Path(raw.decode("utf-8"))
        path = ROOT / relative
        if path.is_file() and not path.is_symlink() and ".runtime" not in relative.parts:
            yield relative, path


def repository_text_files():
    for relative, path in tracked_paths():
        content = path.read_bytes()
        if b"\0" not in content:
            yield relative, content.decode("utf-8", errors="replace")


def code_files():
    for relative, path in tracked_paths():
        if path.suffix in {".py", ".sh"} or relative.parts[0] == "bin":
            yield relative, path


def source_like_expression(node: ast.AST) -> bool:
    rendered = ast.unparse(node).lower()
    return any(token in rendered for token in ("pcloud", "source_remote", "source_spec", "source_path", "args.remote"))


class RepositorySafetyContract(unittest.TestCase):
    def test_download_rejects_all_staging_symlinks_before_write(self) -> None:
        text = (ROOT / "scripts/migration/full-pcloud-download.sh").read_text(encoding="utf-8")
        barrier = text.index("reject_staging_symlinks\n")
        self.assertIn('find -P "$path" -type l -print -quit', text)
        self.assertIn('[ ! -L "$current" ]', text)
        self.assertLess(barrier, text.index('mkdir -p "$PCM_STAGING_DIR"'))
        self.assertLess(barrier, text.index('pcm_rclone_source copy'))

    def test_agents_is_repository_symlink(self) -> None:
        for relative in ("AGENTS.md", "scripts/AGENTS.md", "docs/AGENTS.md"):
            agents = ROOT / relative
            self.assertTrue(agents.is_symlink(), f"{relative} must be a policy symlink")
            self.assertTrue(agents.resolve().is_file())

    def test_public_tree_has_no_personal_addresses_or_key_material(self) -> None:
        email = re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])", re.I)
        ipv4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
        fixed_paths = (
            re.compile(r"/Users/[A-Za-z0-9._-]+", re.I),
            re.compile(r"/home/(?!ubuntu(?:/|$)|debian(?:/|$)|migration-user(?:/|$)|pcloud-proton(?:/|$))[A-Za-z0-9._-]+", re.I),
            re.compile(r"/mnt/transfer[_-]volume", re.I),
        )
        key_patterns = (
            re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
            re.compile(r"\bssh-(?:rsa|ed25519)\s+AAAA[A-Za-z0-9+/]{20,}", re.I),
            re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
            re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"),
            re.compile(r"\b(?:xox[baprs]-[A-Za-z0-9-]{20,})\b"),
        )
        failures: list[str] = []
        for relative, text in repository_text_files():
            for pattern in (*fixed_paths, *key_patterns):
                if pattern.search(text):
                    failures.append(f"{relative} matches {pattern.pattern}")
            for match in email.finditer(text):
                if match.group(0) in PRIVACY_FIXTURE_EXEMPTIONS.get(relative, set()):
                    continue
                domain = match.group(1).lower()
                if domain not in {"example.com", "example.net", "example.org", "invalid"}:
                    failures.append(f"{relative} contains a non-example email address")
            for match in ipv4.finditer(text):
                try:
                    address = ipaddress.ip_address(match.group(0))
                except ValueError:
                    continue
                allowed = address.is_loopback or address.is_unspecified or address in ipaddress.ip_network("192.0.2.0/24") or address in ipaddress.ip_network("198.51.100.0/24") or address in ipaddress.ip_network("203.0.113.0/24")
                if not allowed:
                    failures.append(f"{relative} contains a non-documentation IPv4 address")
        self.assertEqual([], failures)

    def test_no_destructive_provider_invocations_anywhere(self) -> None:
        proton = re.compile(
            r"(?:\bproton-drive\b|\$?PCM_PROTON_BIN\b|\bproton_bin\b)[^\n]{0,240}(?:\s|['\"])(?:delete|trash|purge|overwrite|replace|move|rename|remove|rm)(?:\s|['\"]|$)",
            re.I,
        )
        rclone_mutation = re.compile(r"\brclone\s+(?:[^\s]+\s+){0,8}(?:sync|move|delete|purge|rmdir|mkdir|moveto)\b", re.I)
        failures: list[str] = []
        for relative, text in repository_text_files():
            for number, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if any(line.strip().startswith(prefix) for prefix in DESTRUCTIVE_FIXTURE_PREFIXES.get(relative, ())):
                    continue
                if proton.search(line) or rclone_mutation.search(line):
                    failures.append(f"{relative}:{number}")
        self.assertEqual([], failures, "repository contains a destructive provider invocation")

    def test_python_rclone_arrays_never_write_to_pcloud(self) -> None:
        directional = {"copy", "copyto", "sync", "move", "moveto"}
        source_mutations = {"delete", "deletefile", "mkdir", "purge", "rmdir", "rmdirs"}
        failures: list[str] = []
        for relative, path in code_files():
            if path.suffix != ".py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.List, ast.Tuple)):
                    continue
                values = node.elts
                constants = [item.value.lower() if isinstance(item, ast.Constant) and isinstance(item.value, str) else None for item in values]
                if "rclone" not in constants:
                    continue
                for index, operation in enumerate(constants):
                    if operation in source_mutations and any(source_like_expression(item) for item in values[index + 1 :]):
                        failures.append(f"{relative}:{node.lineno} {operation}")
                    if operation in directional and len(values) > index + 2 and source_like_expression(values[-1]):
                        failures.append(f"{relative}:{node.lineno} {operation} destination")
        self.assertEqual([], failures, "Python rclone command array can mutate pCloud")

    def test_expected_accounts_never_appear_in_process_argv(self) -> None:
        raw_expected_argv = re.compile(
            r"--expected-account(?:=|\s+)[^\n]{0,160}(?:PCM_EXPECTED_(?:PCLOUD|PROTON)_ACCOUNT|expected[_-]account)",
            re.I,
        )
        failures: list[str] = []
        for relative, path in code_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), 1):
                own_fixture = (
                    relative == pathlib.Path("tests/contract/test_repository_safety.py")
                    and line.strip().startswith('r"--expected-account')
                )
                if not own_fixture and raw_expected_argv.search(line):
                    failures.append(f"{relative}:{number}")
            if path.suffix != ".py":
                continue
            tree = ast.parse(text, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.List, ast.Tuple)):
                    continue
                rendered = ast.unparse(node).lower()
                if "--expected-account" in rendered and "expected_account" in rendered:
                    failures.append(f"{relative}:{node.lineno}")
        self.assertEqual([], failures, "raw expected account can be exposed in process argv")

    def test_normal_ci_is_worktree_only(self) -> None:
        workflow = (ROOT / ".github/workflows/offline-contracts.yml").read_text(encoding="utf-8")
        self.assertNotRegex(workflow, r"\bgit\s+(?:log|show|rev-list|fsck)\b")
        release_doc = (ROOT / "docs/release-readiness.md").read_text(encoding="utf-8")
        self.assertIn("Normal CI scans the checked-out tree only", release_doc)
        self.assertIn("reviewed, clean tracked tree", release_doc)


if __name__ == "__main__":
    unittest.main()
