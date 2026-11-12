#!/usr/bin/env python3
"""Drift guard of the root Jenkinsfile's Python test entry (story #3623, task #3677, an accepted addition to story S3).
The guards of the publish pipeline files live in test_site_publish_jenkinsfile.py.
Offline, standard library only. Run: python scripts/test_jenkinsfile.py
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_site import ROOT  # noqa: E402


def read_repo(rel: str) -> str:
    path = ROOT / rel
    if not path.is_file():
        raise AssertionError(f"{rel} does not exist")
    return path.read_text(encoding="utf-8")



def prefixed(problems: list[str], prefix: str) -> list[str]:
    return [p for p in problems if p.startswith(prefix + ":")]


def mutated(original: str, new: str) -> str:
    assert new != original, "the mutation changed nothing"
    return new


BRANCH = "Jenkinsfile"
DISCOVER = 'python3 -m unittest discover -s scripts -p "test_*.py"'
EXISTING_AFTER_BUILD = ["make plugins", "(cd tests/grpc && govulncheck ./...)"]
AFTER_BUILD = re.compile(r"^[ \t]*afterBuild[ \t]*:[ \t]*\[(.*?)\]", re.M | re.S)
QUOTED = re.compile(r"'((?:[^'\\]|\\.)*)'")
SWALLOW = re.compile(r"\|\|\s*(true|:)(?!\w)|;\s*true(?!\w)|set\s+\+e")


def after_build_entries(text: str) -> list[str] | None:
    m = AFTER_BUILD.search(text)
    if not m:
        return None
    return [e.group(1) for e in QUOTED.finditer(m.group(1))]


def branch_problems(text: str) -> list[str]:
    entries = after_build_entries(text)
    if entries is None:
        return ["afterBuild: the go build block has no afterBuild list"]
    problems = []
    for kept in EXISTING_AFTER_BUILD:
        if kept not in entries:
            problems.append(f"afterBuild: the entry {kept!r} was removed")
    runs = [e for e in entries if "unittest discover" in e]
    if DISCOVER not in entries:
        problems.append(f"python: no afterBuild entry is exactly {DISCOVER}")
    for e in runs:
        if SWALLOW.search(e):
            problems.append(f"python: the entry {e!r} swallows a non-zero exit")
    return problems


def discovered_modules() -> tuple[int, set[str]]:
    suite = unittest.TestLoader().discover(str(Path(__file__).resolve().parent), pattern="test_*.py")
    ids = []

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                walk(t)
            else:
                ids.append(t.id())

    walk(suite)
    return len(ids), {i.split(".")[0] for i in ids}


class BranchJenkinsfile(unittest.TestCase):
    """Drift guard of the root Jenkinsfile: an accepted addition to story S3 of the spec, the Definition of Done
    "tests run in CI" (story #3623, task #3677). The Python tests run in the library's Build stage (afterBuild),
    ahead of SonarQube, and a failing test fails the build."""

    def real(self) -> str:
        text = read_repo(BRANCH)
        self.assertEqual(branch_problems(text), [])
        return text

    def test_T1_real_file_runs_discover_in_after_build_and_keeps_the_others(self):
        text = self.real()
        entries = after_build_entries(text)
        self.assertIn(DISCOVER, entries)
        for kept in EXISTING_AFTER_BUILD:
            self.assertIn(kept, entries)

    def test_T1_a_copy_without_the_entry_is_reported(self):
        text = read_repo(BRANCH)
        bad = mutated(text, text.replace(f", '{DISCOVER}'", ""))
        self.assertTrue(prefixed(branch_problems(bad), "python"))

    def test_T1_the_entry_in_a_comment_only_is_reported(self):
        text = read_repo(BRANCH)
        bad = mutated(text, text.replace(f", '{DISCOVER}'", "") + f"\n// '{DISCOVER}'\n")
        self.assertTrue(prefixed(branch_problems(bad), "python"))

    def test_T1_the_entry_in_the_lint_line_is_reported(self):
        text = read_repo(BRANCH)
        stripped = text.replace(f", '{DISCOVER}'", "")
        bad = mutated(text, stripped.replace("lint       : 'fail',", f"lint       : 'fail',  // '{DISCOVER}'"))
        self.assertTrue(prefixed(branch_problems(bad), "python"))

    def test_T1_a_removed_existing_entry_is_reported(self):
        text = read_repo(BRANCH)
        bad = mutated(text, text.replace("'make plugins', ", ""))
        self.assertTrue(prefixed(branch_problems(bad), "afterBuild"))

    def test_T2_real_entry_does_not_swallow_the_exit_code(self):
        text = self.real()
        for entry in after_build_entries(text):
            self.assertIsNone(SWALLOW.search(entry), entry)

    def test_T2_swallowing_variants_are_reported(self):
        text = self.real()
        for suffix in [" || true", " ; true", " || :", " ; set +e"]:
            with self.subTest(suffix=suffix):
                bad = mutated(text, text.replace(f"'{DISCOVER}'", f"'{DISCOVER}{suffix}'"))
                self.assertTrue(branch_problems(bad))

    def test_T3_discover_finds_the_site_test_files(self):
        count, modules = discovered_modules()
        self.assertGreater(count, 0)
        for name in ["test_build_site", "test_site_checks", "test_jenkinsfile", "test_site_publish_steps"]:
            self.assertIn(name, modules)


if __name__ == "__main__":
    unittest.main()
