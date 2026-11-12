#!/usr/bin/env python3
"""Scan tests of the publish job (spec 012-publish-stage, tasks T002 to T004).
PublishScan runs jenkins/publish-steps.sh dry-run (and publish) against a throwaway private repository whose `github`
remote is a local bare repository holding one public commit. It checks that a planted secret stops the run before any
push without a fragment of its value reaching the output, the summary or the report (US1-AS4, US1-AS8, NFR-001), and
that a planted private path is absent from the public result (US1-AS4, D-012).
Needs git, git-filter-repo, node and bash on PATH; otherwise the class is skipped and says which tool is missing.
Planted values are assembled at run time. Offline, standard library only. Run: python scripts/test_site_publish_scan.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_site import ROOT  # noqa: E402

TOOLS = ("git", "git-filter-repo", "node", "bash")
MISSING = [tool for tool in TOOLS if not shutil.which(tool)]
SKIP_REASON = "scan tests need " + ", ".join(TOOLS) + "; not on PATH: " + ", ".join(MISSING)
BASH = shutil.which("bash")
JENKINS = ROOT / "jenkins"
PATTERNS = ".claude/skills/cst-g-commit/scripts/secret-patterns.js"
APPROVER = "approver-example"
GIT_CONFIG = "[user]\n\tname = fixture user\n\temail = fixture@example.invalid\n[init]\n\tdefaultBranch = main\n"
MANAGED_ENV = ("PUBLISH_SSH_KEY", "IM_MIRROR_USER", "IM_SHA_FULL", "PUBLISH_APPROVED_BY", "PUBLISH_DRY_RUN",
               "PUBLISH_GITHUB_SCRIPT", "PUBLISH_DRY_RUN_TIMEOUT")
PUBLIC_FILE = "src/public-readme.txt"
PRIVATE_FILE = "docs/plan/private-roadmap.txt"
SHORT_FILE = "src/short-line.txt"
LONG_FILE = "src/long-line.txt"
RULE_NAME = "GitHub token"
PADDING = "credential for the nightly build agent, rotated monthly and kept off the shared drive: "


def token(*parts: str) -> str:
    """A value the GitHub token rule of secret-patterns.js matches, joined at run time."""
    return "".join(("gh", "p_") + parts)


SHORT_VALUE = token("Zk3Vn8Qw", "2Lx5Ry7T", "b9Hd4Jm6", "Pc1Sf0Ga", "3UeWq8Nz")
LONG_VALUE = token("Rt6Yb2Mc", "9Kd4Fh7J", "w3Ns8Qx5", "Lp1Vg0Ze", "4AuTi7Bo")


class ScanRun:
    def __init__(self, completed: subprocess.CompletedProcess, root: Path, bare: Path, refs_before: str, env: dict):
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.summary = read_file(root / "publish-summary.txt")
        self.report = read_file(root / "publish-dry-run.txt")
        self.refs_before = refs_before
        self.bare = bare
        self.env = env
        self.refs_after = self.public_refs()
        self.files_after = self.public_files()

    def streams(self) -> dict[str, str]:
        return {"stdout": self.stdout, "stderr": self.stderr, "summary": self.summary, "report": self.report}

    def public_refs(self) -> str:
        return git_out(self.env, "--git-dir", str(self.bare), "for-each-ref", "--format=%(refname) %(objectname)")

    def public_files(self) -> list[str]:
        out = git_out(self.env, "--git-dir", str(self.bare), "ls-tree", "-r", "--name-only", "master")
        return out.splitlines()


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def git_out(env: dict, *args: str, cwd: Path | None = None) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                          timeout=120, check=True)
    return done.stdout


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def fixture_env(tmp: Path) -> dict:
    config = tmp / "gitconfig"
    write(config, GIT_CONFIG)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in MANAGED_ENV}
    env.update({"GIT_CONFIG_GLOBAL": config.as_posix(), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})
    return env


def make_public_remote(tmp: Path, env: dict) -> Path:
    bare = tmp / "public.git"
    seed = tmp / "public-seed"
    git_out(env, "init", "--bare", "-b", "master", str(bare))
    git_out(env, "init", "-b", "master", str(seed))
    write(seed / "README.md", "the public fork\n")
    git_out(env, "add", ".", cwd=seed)
    git_out(env, "commit", "-q", "-m", "public commit", cwd=seed)
    git_out(env, "push", "-q", str(bare), "master", cwd=seed)
    return bare


def make_private_repo(root: Path, env: dict, bare: Path, files: dict[str, str]) -> None:
    steps = root / "jenkins"
    steps.mkdir(parents=True)
    shutil.copy(JENKINS / "publish-steps.sh", steps / "publish-steps.sh")
    shutil.copy(JENKINS / "github-known-hosts", steps / "github-known-hosts")
    write(steps / "github-remote", bare.as_posix() + "\n")
    (root / "scripts").mkdir()
    shutil.copy(ROOT / "scripts" / "publish-github.sh", root / "scripts" / "publish-github.sh")
    patterns = root / PATTERNS
    patterns.parent.mkdir(parents=True)
    shutil.copy(ROOT / PATTERNS, patterns)
    write(root / PUBLIC_FILE, "a file that is public\n")
    for name, text in files.items():
        write(root / name, text)
    git_out(env, "init", "-b", "main", cwd=root)
    git_out(env, "add", ".", cwd=root)
    git_out(env, "commit", "-q", "-m", "private history", cwd=root)


def run_publish_steps(command: str, files: dict[str, str] | None = None, approver: str | None = None) -> ScanRun:
    """Runs jenkins/publish-steps.sh <command> from a temporary private repository and returns what it left."""
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        env = fixture_env(tmp)
        bare = make_public_remote(tmp, env)
        root = tmp / "private"
        make_private_repo(root, env, bare, files or {})
        key = tmp / "deploy-key"
        write(key, "-".join(("fake", "key", "body", "not", "real", "456")) + "\n")
        run_env = dict(env)
        run_env.update({"PUBLISH_SSH_KEY": key.as_posix(), "IM_MIRROR_USER": "mirror-user-example",
                        "PUBLISH_GITHUB_SCRIPT": (root / "scripts" / "publish-github.sh").as_posix()})
        if approver is not None:
            run_env["PUBLISH_APPROVED_BY"] = approver
        refs_before = git_out(env, "--git-dir", str(bare), "for-each-ref", "--format=%(refname) %(objectname)")
        completed = subprocess.run([BASH, (root / "jenkins" / "publish-steps.sh").as_posix(), command], cwd=root,
                                   env=run_env, capture_output=True, text=True, encoding="utf-8", timeout=240)
        return ScanRun(completed, root, bare, refs_before, env)


def leaked_fragments(value: str, text: str, width: int = 8) -> list[str]:
    """Every run of `width` characters of the value that appears in the text."""
    return [value[i:i + width] for i in range(len(value) - width + 1) if value[i:i + width] in text]


@unittest.skipIf(MISSING, SKIP_REASON)
class PublishScan(unittest.TestCase):
    """Task T002 fixture; T003 and T004 tests. Covers US1-AS4, US1-AS8, NFR-001."""

    secret_files = {SHORT_FILE: SHORT_VALUE + "\n", LONG_FILE: PADDING + LONG_VALUE + "\n"}

    def test_a_planted_secret_stops_the_dry_run_before_any_push(self):
        """Task T003, US1-AS4 (secret half): non-zero status, public remote refs unchanged."""
        result = run_publish_steps("dry-run", self.secret_files)
        self.assertNotEqual(result.returncode, 0, "the dry run accepted a planted secret")
        self.assertEqual(result.refs_after, result.refs_before)
        self.assertIn("credential-like", result.report)

    def test_the_planted_secret_value_never_reaches_the_output_the_summary_or_the_report(self):
        """Task T003, US1-AS4, US1-AS8, NFR-001: one planted line shorter and one longer than 60 characters."""
        result = run_publish_steps("dry-run", self.secret_files)
        self.assertGreater(len(PADDING + LONG_VALUE), 60)
        self.assertLess(len(SHORT_VALUE), 60)
        for stream in ("stderr", "summary", "report"):
            text = result.streams()[stream]
            with self.subTest(stream=stream):
                self.assertIn(SHORT_FILE, text)
                self.assertIn(LONG_FILE, text)
                self.assertIn(RULE_NAME, text)
        for line, value in (("short line", SHORT_VALUE), ("long line", LONG_VALUE)):
            for stream, text in result.streams().items():
                with self.subTest(stream=stream, line=line):
                    self.assertEqual(leaked_fragments(value, text), [], f"a fragment of the value is in the {stream}")

    def test_a_planted_private_path_is_absent_from_the_public_result(self):
        """Task T004, US1-AS4 (private-path half, D-012, pinned characterisation): stripped by design."""
        dry = run_publish_steps("dry-run", {PRIVATE_FILE: "the private roadmap\n"})
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(dry.refs_after, dry.refs_before)
        real = run_publish_steps("publish", {PRIVATE_FILE: "the private roadmap\n"}, approver=APPROVER)
        self.assertEqual(real.returncode, 0, real.stderr)
        self.assertNotEqual(real.refs_after, real.refs_before, "the real run pushed nothing")
        self.assertIn(PUBLIC_FILE, real.files_after)
        self.assertEqual([f for f in real.files_after if f.startswith("docs/") or "private-roadmap" in f], [])

    def test_a_run_with_both_a_private_path_and_a_planted_secret_still_stops_on_the_secret(self):
        """Task T004, US1-AS4: the secret stops the run before any push; the private path is not the reason."""
        files = dict(self.secret_files)
        files[PRIVATE_FILE] = "the private roadmap\n"
        result = run_publish_steps("dry-run", files)
        self.assertNotEqual(result.returncode, 0, "the dry run accepted a planted secret")
        self.assertEqual(result.refs_after, result.refs_before)
        self.assertIn("credential-like", result.report)
        self.assertNotIn("private-roadmap", result.report)


if __name__ == "__main__":
    unittest.main()
