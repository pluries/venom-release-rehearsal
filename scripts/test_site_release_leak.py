#!/usr/bin/env python3
"""Tests of jenkins/release-leak-check.sh and jenkins/release-leak-patterns.txt (feature 011, task T035: US2-AS7,
FR-015, NFR-005, SC-004). Offline, standard library only; every planted value is assembled at run time.

Contract these tests fix:
- `bash jenkins/release-leak-check.sh <kind> <path>`: kind `body` (path: the release notes file), `binaries` (path: a
  directory, every regular file in it is scanned) or `files` (path: a text file, one image file name per line).
  Status 0 clean, 1 a leak, with one line `leak: <kind> <path>[:<line>]: <pattern name>` per finding on standard error,
  never the matched text of a credential shape.
- The pattern file has the sections `[private-paths]` and `[credential-shapes]`, each followed by one extended regular
  expression per line, `#` lines and blank lines ignored. `body` and `files` apply both sections. `binaries` applies
  neither: it scans with `grep -a` for run-time values only (WORKSPACE, RELEASE_WORK_ROOT, HOME and the host name given
  by `hostname`, empty values skipped), because Go packages embed `-----BEGIN`, `Bearer` and private network address
  literals. A `files` list must also hold an entry named LICENSE and one named NOTICE (the clean lists below do).
Skipped, with the reason printed, when a tool or the private paths are missing.
"""
import getpass
import re
import socket
import unittest

from site_release_fixture import ReleaseCase, canary, requires_release
from test_build_site import ROOT

SCRIPT = "release-leak-check.sh"
PATTERNS = ROOT / "jenkins" / "release-leak-patterns.txt"
SECTIONS = ["private-paths", "credential-shapes"]


def shapes() -> dict:
    """The credential and network shapes of the spec, each assembled from parts."""
    return {"private key block": "-----" + "BEGIN " + "RSA PRIVATE KEY" + "-----",
            "bearer token": "Authorization: " + "Bearer " + "abcdef0123456789abcdef",
            "private address 10": "10." + "20.30." + "40",
            "private address 192": "192." + "168." + "1.20"}


def private_paths() -> dict:
    return {"unix home": "/" + "home" + "/builder/venom/src", "windows drive": "C:" + "\\" + "Users" + "\\builder\\src",
            "jenkins workspace": "/var/lib/" + "jenkins" + "/workspace/venom-release"}


@requires_release
class ReleaseLeak(ReleaseCase):
    """Task T035: the named leak check over the release body, the binaries and the image file list."""

    def setUp(self):
        super().setUp()
        self.workspace = "/ws-" + canary("w", "35") + "/venom"
        self.env = {"WORKSPACE": self.workspace}

    def put(self, name: str, content) -> object:
        path = self.tree.tmp / "scan" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return path

    def check(self, kind: str, path, env: dict | None = None):
        return self.tree.run(kind, [path.as_posix()], script=SCRIPT, env={**self.env, **(env or {})})

    def hostname(self) -> str:
        name = self.tree.shell("hostname").stdout.strip()
        if len(name) < 4:
            self.skipTest("the host name is too short to plant")
        return name

    def test_a_body_naming_a_private_path_stops_the_run(self):
        """Task T035, US2-AS7, FR-015: each private path in a release body is found and the run stops."""
        for label, path in private_paths().items():
            with self.subTest(path=label):
                body = self.put("body.md", f"Release notes\n\nBuilt in {path} on an agent.\n")
                result = self.check("body", body)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertRegex(result.stderr, r"(?m)^leak: body ")

    def test_a_binary_holding_the_workspace_or_host_name_is_found_with_grep_a(self):
        """Task T035, NFR-005: run-time values inside a binary file (NUL bytes included) fail and name that file."""
        host = self.hostname()
        for label, value in (("workspace", self.workspace), ("host name", host)):
            with self.subTest(value=label):
                self.put("venom.linux-amd64", b"\x00\x01clean\x00")
                self.put("venom.linux-arm", b"\x00\x7fELF\x00" + value.encode() + b"\x00\xff")
                result = self.check("binaries", self.tree.tmp / "scan")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("venom.linux-arm", result.stderr)
                self.assertNotIn("venom.linux-amd64", result.stderr)

    def test_a_binary_holding_begin_private_key_bearer_and_a_private_network_address_literal_passes(self):
        """Task T035, plan design 12: the literals Go packages embed are not a leak; an unset run-time value is skipped."""
        literals = b"\x00".join(value.encode() for value in shapes().values())
        self.put("venom.linux-amd64", b"\x7fELF\x00" + literals + b"\x00")
        for env in ({}, {"WORKSPACE": ""}):
            with self.subTest(env=env):
                result = self.check("binaries", self.tree.tmp / "scan", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_credential_shape_patterns_apply_to_the_body_and_the_file_list_only(self):
        """Task T035, FR-015: each shape fails a body and a file list without being echoed, and passes in a binary."""
        for label, shape in shapes().items():
            with self.subTest(shape=label):
                body = self.put("body.md", f"Notes\n\nsee {shape} for details\n")
                listing = self.put("files.txt", f"usr/local/bin/venom\nLICENSE\nNOTICE\netc/{shape}\n")
                binary_dir = self.tree.tmp / "scan-binary"
                binary_dir.mkdir(exist_ok=True)
                (binary_dir / "venom.linux-amd64").write_bytes(b"\x00" + shape.encode() + b"\x00")
                for kind, path in (("body", body), ("files", listing)):
                    result = self.check(kind, path)
                    self.assertEqual(result.returncode, 1, f"{kind}: {result.stdout}")
                    self.assertNotIn(shape, result.output(), "the matched shape was echoed")
                self.assertEqual(self.check("binaries", binary_dir).returncode, 0)

    def test_a_file_list_with_a_private_path_is_found(self):
        """Task T035, US2-AS7, NFR-005: a private path in the image file list stops the run."""
        for label, path in private_paths().items():
            with self.subTest(path=label):
                listing = self.put("files.txt", f"usr/local/bin/venom\nLICENSE\nNOTICE\n{path}/.cache/entry\n")
                result = self.check("files", listing)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertRegex(result.stderr, r"(?m)^leak: files ")

    def test_a_file_list_entry_without_a_leading_slash_is_found(self):
        """Review finding 1: a `docker export | tar -t` list has no leading slash; a private path there is a leak."""
        for label, path in private_paths().items():
            if not path.startswith("/"):
                continue
            with self.subTest(path=label):
                listing = self.put("files.txt", f"usr/local/bin/venom\nLICENSE\nNOTICE\n{path.lstrip('/')}/.cache/entry\n")
                result = self.check("files", listing)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertRegex(result.stderr, r"(?m)^leak: files ")

    def test_a_clean_body_binary_and_list_pass(self):
        """Task T035, SC-004: ordinary notes, a binary with no run-time value and a list with LICENSE pass."""
        body = self.put("body.md", "Release v1.4.0-pluries.2\n\nBuilt from the public tag. Verify with openssl cms.\n")
        self.assertEqual(self.check("body", body).returncode, 0)
        self.put("venom.linux-amd64", b"\x7fELF\x00some code\x00")
        self.assertEqual(self.check("binaries", self.tree.tmp / "scan").returncode, 0)
        listing = self.put("files.txt", "usr/local/bin/venom\nLICENSE\nNOTICE\nworkdir/\n")
        self.assertEqual(self.check("files", listing).returncode, 0)

    def test_a_file_list_without_a_licence_or_without_a_notice_stops_the_run(self):
        """Task T079, NFR-008: each of the two entries is required on its own; the message names the missing one."""
        for present, missing in (("LICENSE", "NOTICE"), ("NOTICE", "LICENSE")):
            with self.subTest(missing=missing):
                listing = self.put("files.txt", f"usr/local/bin/venom\n{present}\nworkdir/\n")
                result = self.check("files", listing)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertRegex(result.stderr, rf"(?m)^leak: files .*: no entry named {missing}$")
                self.assertNotIn(f"no entry named {present}", result.stderr)

    def test_a_notice_entry_in_a_subdirectory_counts_but_a_longer_name_does_not(self):
        """Task T079: the entry is matched as `(^|/)NOTICE$`: usr/share/licenses/venom/NOTICE passes, NOTICE.txt does not."""
        ok = self.put("ok.txt", "usr/share/licenses/venom/LICENSE\nusr/share/licenses/venom/NOTICE\n")
        self.assertEqual(self.check("files", ok).returncode, 0)
        longer = self.put("longer.txt", "LICENSE\nNOTICE.txt\nMY-NOTICE\n")
        refused = self.check("files", longer)
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("no entry named NOTICE", refused.stderr)

    def sections_of(self, text: str) -> dict:
        found, current = {}, None
        for line in text.splitlines():
            match = re.fullmatch(r"\[([a-z-]+)\]", line.strip())
            if match:
                current = found.setdefault(match.group(1), [])
            elif current is not None and line.strip() and not line.lstrip().startswith("#"):
                current.append(line.strip())
        return found

    def test_the_patterns_file_has_one_section_for_body_and_files_and_holds_no_real_host_name_or_address(self):
        """Task T035, plan design 12: both sections exist once, hold valid expressions, no address or machine name."""
        text = PATTERNS.read_text(encoding="utf-8")
        headers = re.findall(r"(?m)^\[([a-z-]+)\]$", text)
        self.assertEqual(headers, SECTIONS)
        found = self.sections_of(text)
        for name in SECTIONS:
            self.assertTrue(found[name], f"section [{name}] holds no pattern")
        for pattern in [p for lines in found.values() for p in lines]:
            with self.subTest(pattern=pattern):
                probe = self.tree.shell('grep -Eq -e "$1" /dev/null; test $? -lt 2', pattern)
                self.assertEqual(probe.returncode, 0, "not an extended regular expression")
        self.assertNotRegex(text, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        for real in {socket.gethostname(), socket.getfqdn(), getpass.getuser()}:
            if len(real) >= 4:
                self.assertNotIn(real.lower(), text.lower())


if __name__ == "__main__":
    unittest.main()
