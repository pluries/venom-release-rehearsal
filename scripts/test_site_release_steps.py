#!/usr/bin/env python3
"""Tests of jenkins/release-lib.sh and jenkins/release-steps.sh (feature 011, tasks T004, T006 to T013, T026 to T029,
T036 and T037).
ReleaseLib (T004), ReleaseResolve (T006, T007), ReleaseBuild (T008 to T011), ReleaseReproduce (T012) and
ReleaseDeadline (T013) drive the scripts against a local bare "public repository" with stub make, go and a recording
git wrapper first on PATH (scripts/site_release_fixture.py). No test needs cross-compilation, Docker or a key.

Contract these tests fix (the scripts are written to it, never the other way round):
- Every command is `bash jenkins/release-steps.sh <command> [args]`, run from the tree root; parameters are in the
  environment (RELEASE_PUBLIC_URL, RELEASE_TAG, RELEASE_SHA, RELEASE_WORK_ROOT, JOB_NAME, BUILD_NUMBER,
  RELEASE_DEADLINE, RELEASE_BASE); a refusal exits 1 with the reason on standard error, "not implemented" is exit 2.
- release-lib.sh: validators valid_tag, valid_sha, valid_approver, valid_api_base, valid_path return 0 or 1 and print
  nothing on success; work_dir prints <RELEASE_WORK_ROOT>/<slug of JOB_NAME>/<BUILD_NUMBER>; the array EXPECTED_FILES
  holds the ten names.
- Inside the work directory: src/ (the anonymous clone at RELEASE_SHA), dist/ (the ten files, then SHA256SUMS, lines
  "<hash>  <name>" in C-locale order) and record-input (key=value lines, `go_version=go1.27.3`).
- `deadline <start-epoch> <seconds> [<wait-seconds>...]` prints start + seconds + the waits; `remaining <deadline-epoch>`
  prints the seconds left and exits 1 when none are left; a long command with RELEASE_DEADLINE in the past exits 1
  before it starts any work.
- jenkins/release-repro-exceptions.txt lines are `<file> <reason> <approved-by> <date>`.
- US2 (T026 to T029, T036, T037; D-059: Certum signs SHA256SUMS, minisign is dropped): sign-verify (the detached CMS
  signature dist/SHA256SUMS.p7s and the pinned signer of jenkins/release-certificate.json), timestamp (RELEASE_TSA_URL,
  RELEASE_TSA_CAFILE), checks, check-approver <name> (RELEASE_APPROVERS), record. The contract of each is in the
  docstring of its test class.
Skipped, with the reason printed, when bash, git, node, sha256sum or curl is missing or the private paths are absent
(the public tree has no jenkins/release-*.sh). Planted values are assembled at run time. Offline, standard library only.
"""
import datetime
import hashlib
import re
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from site_release_fixture import (EXPECTED_NAMES, IS_MSYS, SIGNER_SHA256, TAG, TSA_CURL_STUB, FakeGitHub, ReleaseCase,
                                  canary, fake_tsr, requires_release, write)

MSYS_ARGUMENT_REASON = "MSYS bash truncates arguments at a newline and drops quotes"
MSYS_ARGUMENT_LOSS = {("valid_tag", "v1.4.0-pluries.1\n"), ("valid_sha", "a" * 40 + "\n"),
                      ("valid_approver", "a\nb"), ("valid_path", "/tmp/a'b")}

PLANTED = {"PLANTED_TOKEN": canary("t", "1"), "PLANTED_KEY_FILE": canary("k", "2"),
           "PLANTED_PASSWORD": canary("p", "3"), "PLANTED_SECRET": canary("s", "4"),
           "RELEASE_NOTES": canary("n", "5")}
DIFFERING = "venom.linux-arm"
APPROVERS = "maker-example,second.user"
TODAY = datetime.date.today().isoformat()


def named(name: str, text: str) -> bool:
    """True when the file name appears in the text and is not just the prefix of a longer name."""
    return re.search(re.escape(name) + r"(?![\w-])", text) is not None


def inspected_names(result) -> list:
    names = []
    for call in result.calls("go"):
        if call.argv[:2] == ["version", "-m"]:
            names += [Path(arg).name for arg in call.argv[2:]]
    return names


def go_matrix() -> list | None:
    """The ten names the Makefile matrix gives on `go tool dist list` (the filters of .build/go.mk), or None."""
    go = shutil.which("go")
    if not go:
        return None
    listing = subprocess.run([go, "tool", "dist", "list"], capture_output=True, text=True, timeout=60).stdout.split()
    excluded = re.compile(r"^(darwin/386|windows/386|windows/arm|openbsd/ar|openbsd/386|freebsd/ar|freebsd/386)")
    names = []
    for pair in listing:
        os_name, _, arch = pair.partition("/")
        if os_name in ("windows", "darwin", "linux", "openbsd", "freebsd") and \
                arch in ("amd64", "arm", "386", "arm64", "ppc64le") and not excluded.match(pair):
            names.append(f"venom.{os_name}-{arch}" + (".exe" if os_name == "windows" else ""))
    return names


@requires_release
class ReleaseLib(ReleaseCase):
    """Task T004: the shared helpers of release-lib.sh."""

    def assert_accepts(self, function: str, values: list) -> None:
        for value in values:
            with self.subTest(function=function, value=value):
                result = self.tree.lib(function, value)
                self.assertEqual(result.returncode, 0, f"{function} refused {value!r}: {result.stderr}")

    def assert_refuses(self, function: str, values: list) -> None:
        for value in values:
            with self.subTest(function=function, value=value):
                if IS_MSYS and (function, value) in MSYS_ARGUMENT_LOSS:
                    self.skipTest(MSYS_ARGUMENT_REASON)
                result = self.tree.lib(function, value)
                self.assertEqual(result.returncode, 1, f"{function} accepted {value!r}: {result.stderr}")

    def test_a_tag_must_match_v_base_pluries_n(self):
        """Task T004, FR-001: v<major>.<minor>.<patch>-pluries.<n>, n from 1, nothing else."""
        self.assert_accepts("valid_tag", ["v1.4.0-pluries.1", "v10.20.30-pluries.12"])
        self.assert_refuses("valid_tag", ["1.4.0-pluries.1", "v1.4.0", "v1.4.0-pluries.0", "v1.4-pluries.1",
                                          "v1.4.0-pluries.", "v1.4.0-pluries.1\n", "v1.4.0-pluries.1 x",
                                          "v1.4.0-pluries.1;x", "", "V1.4.0-pluries.1", "v1.4.0-pluries.01"])

    def test_a_sha_must_be_forty_lowercase_hex(self):
        """Task T004, FR-010: a full lowercase commit id only."""
        self.assert_accepts("valid_sha", ["a" * 40, "0123456789abcdef" * 2 + "01234567"])
        self.assert_refuses("valid_sha", ["A" * 40, "a" * 39, "a" * 41, "g" * 40, "", "a" * 40 + "\n"])

    def test_an_approver_must_match_the_allowed_characters(self):
        """Task T004, NFR-002: ^[A-Za-z0-9._@-]{1,80}$, the rule of spec 012."""
        self.assert_accepts("valid_approver", ["maker-example", "a.b_c@d", "A" * 80, "x"])
        self.assert_refuses("valid_approver", ["", "A" * 81, "a b", "a;b", "a/b", 'a"b', "a\nb"])

    def test_the_api_base_must_be_api_github_com_or_loopback(self):
        """Task T004, NFR-001: no other host can receive the token."""
        self.assert_accepts("valid_api_base", ["https://api.github.com", "http://127.0.0.1:8123"])
        self.assert_refuses("valid_api_base", ["https://evil.test", "https://api.github.com.evil.test",
                                               "https://api.github.com@evil.test", "http://127.0.0.1.evil.test",
                                               "http://127.0.0.1@evil.test", "ftp://api.github.com", ""])

    def test_a_path_with_a_quote_a_space_or_dotdot_is_refused(self):
        """Task T004, NFR-001: a path that reaches a shell command or leaves its root is refused."""
        self.assert_accepts("valid_path", ["/var/work/venom-release/1.2"])
        self.assert_refuses("valid_path", ["/tmp/a b", '/tmp/a"b', "/tmp/a'b", "/tmp/../x", "..", "/tmp/a/..", ""])

    def test_the_work_directory_is_derived_from_job_name_and_build_number_not_from_workspace_and_a_bad_slug_or_number_is_refused(self):
        """Task T004, design 1 (work directory convention, open point 22)."""
        env = {"RELEASE_WORK_ROOT": "/var/release-work", "JOB_NAME": "venom/venom-release", "BUILD_NUMBER": "42"}
        first = self.tree.lib("work_dir", env={**env, "WORKSPACE": "/ws/one"})
        second = self.tree.lib("work_dir", env={**env, "WORKSPACE": "/ws/two@2"})
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertRegex(first.stdout.strip(), r"^/var/release-work/[A-Za-z0-9._-]+/42$")
        self.assertIn("venom-release", first.stdout)
        self.assertEqual(second.stdout, first.stdout, "the work directory followed WORKSPACE")
        for key, bad in (("JOB_NAME", ".."), ("JOB_NAME", "a b"), ("JOB_NAME", ""), ("JOB_NAME", "x;y"),
                         ("BUILD_NUMBER", "4x"), ("BUILD_NUMBER", ""), ("BUILD_NUMBER", "../1"), ("BUILD_NUMBER", "-1")):
            with self.subTest(**{key: bad}):
                refused = self.tree.lib("work_dir", env={**env, key: bad})
                self.assertEqual(refused.returncode, 1, refused.stdout)

    def test_the_expected_list_has_ten_names_and_equals_the_matrix_of_the_makefile(self):
        """Task T004, FR-015: the array of ten names, and (with go on PATH) the matrix of .build/go.mk."""
        result = self.tree.shell('. jenkins/release-lib.sh; printf "%s\\n" "${EXPECTED_FILES[@]}"')
        names = [line for line in result.stdout.splitlines() if line]
        self.assertEqual(len(names), 10, names)
        self.assertEqual(sorted(names), sorted(EXPECTED_NAMES))
        matrix = go_matrix()
        if matrix is None:
            self.skipTest("go is not on PATH: the Makefile matrix was not compared")
        self.assertEqual(sorted(names), sorted(matrix))


@requires_release
class ReleaseResolve(ReleaseCase):
    """Tasks T006 and T007: public-tip, propose, check-tag against a local bare public repository."""

    def test_public_tip_prints_one_full_sha_and_nothing_else(self):
        """Task T006, US1-AS1: standard output is the tip of the public master and a newline."""
        result = self.tree.run("public-tip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, self.tree.sha_tip + "\n")

    def test_propose_is_base_pluries_next_number_from_the_public_tags(self):
        """Task T006, US1-AS1: the next number after the highest public tag of the base."""
        self.tree.public_tag("v1.4.0-pluries.1")
        self.tree.public_tag("v1.4.0-pluries.2", annotated=True)
        result = self.tree.run("propose")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "v1.4.0-pluries.3\n")

    def test_propose_with_no_pluries_tag_needs_a_typed_base_and_starts_at_one(self):
        """Task T006, US1-AS1: no public pluries tag, no guess; a typed base gives n = 1; a bad base is refused."""
        self.tree.public_tag("v2.0.0")
        refused = self.tree.run("propose")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("base", refused.stderr.lower())
        typed = self.tree.run("propose", env={"RELEASE_BASE": "1.5.0"})
        self.assertEqual((typed.returncode, typed.stdout), (0, "v1.5.0-pluries.1\n"), typed.stderr)
        bad = self.tree.run("propose", env={"RELEASE_BASE": "1.5"})
        self.assertEqual(bad.returncode, 1, bad.stdout)

    def test_the_base_is_taken_from_the_highest_public_pluries_tag_and_not_from_git_history(self):
        """Task T006, FR-001 and open point 31: versions compare as numbers, other tags and commit texts are ignored."""
        for tag in ("v1.9.0-pluries.14", "v1.10.0-pluries.2", "v1.10.0-pluries.10", "v9.9.9"):
            self.tree.public_tag(tag, annotated=tag.endswith(".10"))
        result = self.tree.run("propose")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "v1.10.0-pluries.11\n")

    def test_a_typed_tag_that_does_not_match_the_pattern_is_refused(self):
        """Task T006, US1-AS1: check-tag refuses a typed tag outside v<base>-pluries.<n> and says so."""
        for typed in ("1.4.0-pluries.1", "v1.4.0", "v1.4.0-pluries.0", "v1;rm", "v1.4.0-pluries.1 x", ""):
            with self.subTest(tag=typed):
                result = self.tree.run("check-tag", [typed])
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("pattern", result.stderr.lower())

    def test_check_tag_stops_when_the_public_repository_has_the_tag(self):
        """Task T007, US1-AS2: an existing public tag stops the run; another tag goes on."""
        self.tree.public_tag("v1.4.0-pluries.2", annotated=True)
        taken = self.tree.run("check-tag", ["v1.4.0-pluries.2"])
        self.assertEqual(taken.returncode, 1, taken.stdout)
        self.assertIn("exists", taken.stderr.lower())
        free = self.tree.run("check-tag", ["v1.4.0-pluries.3"])
        self.assertEqual(free.returncode, 0, free.stderr)

    def hiding(self, tool: str | None) -> dict:
        """BASH_ENV that makes `command -v <tool>` find nothing, whatever the real PATH holds (go, make and docker are
        stubs here, but a real one may sit later on PATH)."""
        hide = self.tree.tmp / "hide-tool.sh"
        write(hide, 'command() { if [ "$1" = -v ] && [ "$2" = "' + (tool or "none") + '" ]; then return 1; fi; '
                    'builtin command "$@"; }' + "\n")
        return {"BASH_ENV": hide.as_posix()}

    def test_setup_names_a_missing_tool(self):
        """Task T016 (review finding 9): setup stops and names the one missing tool; with every tool present it passes."""
        ok = self.tree.run("setup", env=self.hiding(None))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        for tool in ("git", "go", "make", "node", "curl", "timeout", "docker", "sha256sum"):
            with self.subTest(tool=tool):
                result = self.tree.run("setup", env=self.hiding(tool))
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertRegex(result.stderr, rf"(?m)missing tool.*(?<![\w-]){tool}(?![\w-])")

    def test_check_tag_stops_on_a_retired_tag_marker(self):
        """Task T007, NFR-010: a docs/release/<tag>/WITHDRAWN file retires the tag for good, public tag or not."""
        self.tree.write_private("docs/release/v1.4.0-pluries.5/WITHDRAWN", "withdrawn: planted for the test\n")
        retired = self.tree.run("check-tag", ["v1.4.0-pluries.5"])
        self.assertEqual(retired.returncode, 1, retired.stdout)
        self.assertRegex(retired.stderr.lower(), "withdrawn|retired")
        free = self.tree.run("check-tag", ["v1.4.0-pluries.6"])
        self.assertEqual(free.returncode, 0, free.stderr)


class BuildCase(ReleaseCase):
    """Common steps of the build classes: a build, and what a clean build must leave in every recorded call."""

    def built(self, tree=None) -> None:
        tree = tree or self.tree
        result = tree.run("build")
        self.assertEqual(result.returncode, 0, result.stderr)

    def dist(self, tree=None) -> Path:
        return (tree or self.tree).work_dir() / "dist"


@requires_release
class ReleaseBuild(BuildCase):
    """Tasks T008 to T011: build, verify-build and sums against stub make and go."""

    def test_build_clones_the_public_repository_anonymously_at_the_sha(self):
        """Task T008, US1-AS3, FR-002: a plain clone of the public URL, no credential, checked out at the SHA."""
        result = self.tree.run("build", env=PLANTED)
        self.assertEqual(result.returncode, 0, result.stderr)
        clones = [c for c in result.calls("git") if "clone" in c.argv]
        self.assertTrue(clones, "no git clone was recorded")
        for call in clones:
            self.assertIn(self.tree.public.as_posix(), call.argv)
            self.assertEqual([a for a in call.argv if "extraheader" in a or "credential" in a or "@" in a], [])
            self.assertEqual([n for n in call.env if re.search("TOKEN|KEY|PASSWORD|SECRET", n)], [])
        head = subprocess.run(["git", "-C", str(self.tree.work_dir() / "src"), "rev-parse", "HEAD"],
                              capture_output=True, text=True, env=self.tree.git_env).stdout.strip()
        self.assertEqual(head, self.tree.sha_first)
        self.assertNotEqual(head, self.tree.sha_tip)

    def test_make_gets_the_tag_as_git_describe_and_an_environment_with_no_token_key_password_or_release_variable(self):
        """Task T008, FR-002, NFR-013: the planted variable of each kind is absent from the recorded environment."""
        result = self.tree.run("build", env=PLANTED)
        self.assertEqual(result.returncode, 0, result.stderr)
        makes = result.calls("make")
        self.assertEqual(len(makes), 1, "one make build is expected")
        self.assertIn("build", makes[0].argv)
        self.assertIn(f"GIT_DESCRIBE={TAG}", makes[0].argv)
        for call in makes + result.calls("go"):
            with self.subTest(argv=call.argv):
                self.assertEqual(call.secret_names(), [])
                for value in PLANTED.values():
                    self.assertNotIn(value, "".join(call.env.values()))

    def test_verify_build_requires_the_tag_in_the_build_info_of_each_of_the_ten_files(self):
        """Task T008, US1-AS3: go version -m of each file shows the tag; a file with another tag fails and is named."""
        self.built()
        good = self.tree.run("verify-build")
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(sorted(set(inspected_names(good))), sorted(EXPECTED_NAMES))
        other = self.new_tree(STUB_GO_WRONG_TAG_FILE="venom.freebsd-amd64")
        self.built(other)
        bad = other.run("verify-build")
        self.assertEqual(bad.returncode, 1, bad.stdout)
        self.assertTrue(named("venom.freebsd-amd64", bad.stderr), bad.stderr)

    def test_verify_build_runs_the_native_version_command_when_the_host_matches(self):
        """Task T008, US1-AS3: on a linux/amd64 host the linux-amd64 file runs `version` and prints the tag."""
        self.built()
        result = self.tree.run("verify-build")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" version", self.tree.native_calls())
        other = self.new_tree(STUB_NATIVE_VERSION="v9.9.9")
        self.built(other)
        bad = other.run("verify-build")
        self.assertEqual(bad.returncode, 1, bad.stdout)
        self.assertTrue(named("venom.linux-amd64", bad.stderr), bad.stderr)

    def test_build_sets_gotoolchain_local_and_fails_when_go_version_is_below_the_go_line_of_go_mod(self):
        """Task T008, F8, open point 32: GOTOOLCHAIN=local in every go and make call; go 1.26.9 < 1.27.3 stops."""
        result = self.tree.run("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        for call in result.calls("go") + result.calls("make"):
            with self.subTest(argv=call.argv):
                self.assertEqual(call.env.get("GOTOOLCHAIN"), "local")
        old = self.new_tree(STUB_GO_VERSION="1.26.9")
        refused = old.run("build")
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertEqual(refused.calls("make"), [], "make ran with a Go older than go.mod")
        self.assertRegex(refused.stderr, r"(?i)go\.mod|go version")
        newer = self.new_tree(STUB_GO_VERSION="1.27.10")
        self.assertEqual(newer.run("build").returncode, 0, "1.27.10 is not below 1.27.3")

    def test_a_binary_holding_the_clone_path_fails_the_check_and_names_the_file(self):
        """Task T009, US1-AS4, NFR-005: a build path inside a binary fails verify-build and names that file."""
        tree = self.new_tree(STUB_MAKE_EMBED_PWD="venom.linux-386")
        self.built(tree)
        result = tree.run("verify-build")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(named("venom.linux-386", result.stderr), result.stderr)
        self.assertFalse(named("venom.darwin-arm64", result.stderr), "a clean file was named")

    def test_a_build_info_without_trimpath_or_with_cgo_or_with_a_vcs_time_fails(self):
        """Task T009, US1-AS4, NFR-006: each of the three build-info faults fails and names the file."""
        for kind, word in (("trimpath", "trimpath"), ("cgo", "cgo"), ("vcs", "vcs")):
            with self.subTest(kind=kind):
                tree = self.new_tree(STUB_GO_BAD_FILE="venom.darwin-arm64", STUB_GO_BAD_KIND=kind)
                self.built(tree)
                result = tree.run("verify-build")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertTrue(named("venom.darwin-arm64", result.stderr), result.stderr)
                self.assertIn(word, result.stderr.lower())

    def test_the_go_version_is_written_to_the_record_input(self):
        """Task T009, NFR-004: the agent's Go version reaches work/record-input as go_version=."""
        self.built()
        self.assertEqual(self.tree.run("verify-build").returncode, 0)
        record = (self.tree.work_dir() / "record-input").read_text(encoding="utf-8")
        self.assertRegex(record, r"(?m)^go_version=go1\.27\.3$")

    def test_go_mod_verify_runs_with_readonly_modules_and_a_changed_go_sum_fails_the_build(self):
        """Task T010, US1-AS9, FR-013: go mod verify under GOFLAGS=-mod=readonly; a modified go.sum stops before make."""
        result = self.tree.run("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        verifies = [c for c in result.calls("go") if c.argv[:2] == ["mod", "verify"]]
        self.assertEqual(len(verifies), 1)
        self.assertIn("-mod=readonly", verifies[0].env.get("GOFLAGS", ""))
        touched = self.new_tree(STUB_GO_TOUCH_GOSUM="1")
        stopped = touched.run("build")
        self.assertEqual(stopped.returncode, 1, stopped.stdout)
        self.assertIn("go.sum", stopped.stderr)
        self.assertEqual(stopped.calls("make"), [], "make ran after go.sum changed")

    def test_sums_lists_exactly_the_ten_files_sorted_in_the_c_locale_and_passes_sha256sum_check(self):
        """Task T011, US1-AS5, FR-003: SHA256SUMS over the ten files only, in code-point order, verifiable."""
        self.built()
        dist = self.dist()
        (dist / "notes.txt").write_text("not a release file\n", encoding="utf-8")
        (dist / "venom.linux-mips").write_text("not an expected name\n", encoding="utf-8")
        result = self.tree.run("sums")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = (dist / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        parsed = [re.fullmatch(r"([0-9a-f]{64})  (venom\.[\w.-]+)", line) for line in lines]
        self.assertNotIn(None, parsed, lines)
        names = [m.group(2) for m in parsed]
        self.assertEqual(names, sorted(EXPECTED_NAMES))
        for match in parsed:
            self.assertEqual(match.group(1), hashlib.sha256((dist / match.group(2)).read_bytes()).hexdigest())
        check = subprocess.run(["sha256sum", "-c", "SHA256SUMS"], cwd=dist, capture_output=True, text=True)
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)


@requires_release
class ReleaseReproduce(BuildCase):
    """Task T012: reproduce builds the same SHA again and compares the two SHA256SUMS."""

    def prepared(self, **config):
        tree = self.new_tree(**config)
        self.built(tree)
        self.assertEqual(tree.run("sums").returncode, 0)
        return tree

    def exceptions(self, tree, text: str) -> None:
        tree.write_private("jenkins/release-repro-exceptions.txt", "# <file> <reason> <approved-by> <date>\n" + text)

    def test_two_identical_builds_pass(self):
        """Task T012, US1-AS6: identical hashes, status 0."""
        tree = self.prepared()
        result = tree.run("reproduce")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.calls("make")), 2, "the second build did not run")

    def test_one_differing_file_fails_and_is_named(self):
        """Task T012, US1-AS6, NFR-006: the file whose second build differs is named, the others are not."""
        tree = self.prepared(STUB_MAKE_DIFFER=DIFFERING)
        result = tree.run("reproduce")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(named(DIFFERING, result.stderr), result.stderr)
        self.assertFalse(named("venom.linux-386", result.stderr), "an identical file was named")

    def test_a_named_exception_with_four_fields_passes_and_is_printed(self):
        """Task T012, US1-AS6: file, reason, approver and date accepted; the exception is printed for the record."""
        tree = self.prepared(STUB_MAKE_DIFFER=DIFFERING)
        self.exceptions(tree, f"{DIFFERING} embedded-build-id maker-example {TODAY}\n")
        result = tree.run("reproduce")
        self.assertEqual(result.returncode, 0, result.stderr)
        for word in (DIFFERING, "embedded-build-id", "maker-example", TODAY):
            self.assertIn(word, result.stdout)

    def test_an_exception_for_another_file_or_with_a_missing_field_is_refused(self):
        """Task T012, US1-AS6, design 7: an exception for another file, three fields, or a date before the tag fails."""
        lines = {"another file": f"venom.linux-386 embedded-build-id maker-example {TODAY}\n",
                 "three fields": f"{DIFFERING} embedded-build-id maker-example\n",
                 "outside the ten": f"venom.linux-mips embedded-build-id maker-example {TODAY}\n",
                 "date too old": f"{DIFFERING} embedded-build-id maker-example 2001-01-01\n"}
        for label, line in lines.items():
            with self.subTest(case=label):
                tree = self.prepared(STUB_MAKE_DIFFER=DIFFERING)
                self.exceptions(tree, line)
                result = tree.run("reproduce")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertTrue(named(DIFFERING, result.stderr), result.stderr)


@requires_release
class ReleaseDeadline(ReleaseCase):
    """Task T013: deadline, remaining and the refusal of a long command with no time left (NFR-007)."""

    def test_remaining_counts_down_and_fails_when_the_budget_is_spent(self):
        """Task T013, US1-AS7: seconds left while there are some; status 1 and nothing printed when spent."""
        now = int(time.time())
        left = self.tree.run("remaining", [str(now + 120)])
        self.assertEqual(left.returncode, 0, left.stderr)
        self.assertTrue(100 <= int(left.stdout.strip()) <= 120, left.stdout)
        for epoch in (now, now - 5):
            with self.subTest(deadline=epoch - now):
                spent = self.tree.run("remaining", [str(epoch)])
                self.assertEqual(spent.returncode, 1, spent.stdout)
                self.assertEqual(spent.stdout.strip(), "")

    def test_wait_seconds_are_added_to_the_deadline(self):
        """Task T013, US1-AS7: the approval wait and the Windows pause do not count against the budget."""
        for args, expected in ((["1000", "3600"], "4600"), (["1000", "3600", "600"], "5200"),
                               (["1000", "3600", "600", "300"], "5500")):
            with self.subTest(args=args):
                result = self.tree.run("deadline", args)
                self.assertEqual((result.returncode, result.stdout.strip()), (0, expected), result.stderr)

    def test_a_long_command_that_finds_no_time_left_exits_non_zero_before_starting_work(self):
        """Task T013, US1-AS7: build with a spent RELEASE_DEADLINE clones nothing and runs no tool."""
        result = self.tree.run("build", env={"RELEASE_DEADLINE": "1"})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("deadline", result.stderr.lower())
        for stub in ("git", "go", "make"):
            self.assertEqual(result.calls(stub), [], f"{stub} ran with no time left")
        self.assertFalse(self.tree.work_root.exists() and list(self.tree.work_root.glob("*/1/src")))


def fault(result, kind: str, detail: str) -> bool:
    """True when stderr holds the line `fail <kind> <detail>` (the line format of the checks command)."""
    return re.search(rf"(?m)^fail {kind} {re.escape(detail)}\b", result.stderr) is not None


def files_holding(roots: list, value: str) -> list:
    """Every file under the roots whose bytes hold the value."""
    needle = value.encode()
    return [p for root in roots for p in Path(root).rglob("*") if p.is_file() and needle in p.read_bytes()]


def field(text: str, label: str) -> str | None:
    """The value of the record line `- <label>: <value>`, or None."""
    match = re.search(rf"(?m)^- {re.escape(label)}: (.*)$", text)
    return match.group(1).strip() if match else None


def calls_with(result, stub: str, *words: str) -> list:
    """The recorded calls of a stub whose argv holds every word."""
    return [c for c in result.calls(stub) if all(w in c.argv for w in words)]


def after(call, flag: str) -> str:
    """The argv element that follows the flag, Windows separators turned into slashes."""
    return call.argv[call.argv.index(flag) + 1].replace("\\", "/")


def recorded(tree) -> str:
    path = tree.work_dir() / "record-input"
    return path.read_text(encoding="utf-8") if path.is_file() else ""


@requires_release
class ReleaseSignVerify(ReleaseCase):
    """Task T026 (D-059 supersedes D-009, US2-AS1, FR-003, FR-013, NFR-002): `sign-verify` checks the detached CMS
    signature <work>/dist/SHA256SUMS.p7s that the windows-sign agent made, with
    `openssl cms -verify -inform DER -in <p7s> -content <SHA256SUMS> -binary -purpose any -signer <tmp>/signer.pem
    -out /dev/null`, then compares the SHA-256 of the signer certificate (`openssl x509 -in <signer.pem> -noout
    -fingerprint -sha256`, lowercased, colons removed) with the sha256 field of jenkins/release-certificate.json.
    Success prints `signature check: ok SHA256SUMS signer <sha256>` and records signature_check=ok and
    signer_sha256=<sha256>; a failure exits 1 and its message names `signature` (the CMS verify failed, the file is
    missing or empty) or `signer` (another certificate signed). The openssl stub of the fixture checks the p7s against
    the bytes of SHA256SUMS, so a changed byte fails it as it fails the real tool. The old minisign tests (sign, key,
    fingerprint, password on standard input) are removed by owner decision D-059."""

    def verified(self, certificate: bool = True, **config):
        tree = self.new_tree(**config)
        tree.prepare_release_files()
        if certificate:
            tree.write_certificate()
        return tree, tree.run("sign-verify")

    def test_a_good_signature_by_the_pinned_signer_passes_prints_the_check_and_records_both_facts(self):
        """Task T026, FR-003, D-059: status 0, `signature check: ok SHA256SUMS signer <sha256>`, record-input holds
        signature_check=ok and signer_sha256=<the pinned sha256>."""
        tree, result = self.verified()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, rf"(?m)^signature check: ok SHA256SUMS signer {SIGNER_SHA256}$")
        record = recorded(tree)
        self.assertRegex(record, r"(?m)^signature_check=ok$")
        self.assertRegex(record, rf"(?m)^signer_sha256={SIGNER_SHA256}$")

    def test_the_verify_runs_openssl_cms_on_the_p7s_and_sha256sums_and_asks_the_signer_certificate_for_its_sha256(self):
        """Task T026, D-059: one `openssl cms -verify` with the DER p7s, the detached content, -binary, `-purpose any`
        and a signer file; then `openssl x509 -noout -fingerprint -sha256` on that same file; neither tool sees a
        credential-like variable (NFR-002, NFR-013). The stub prints the uppercase colon form (AA:BB:..) and the pin
        is lowercase hex, so the pass of the first test also proves the normalisation."""
        tree, result = self.verified()
        self.assertEqual(result.returncode, 0, result.stderr)
        verifies = calls_with(result, "openssl", "cms", "-verify")
        self.assertEqual(len(verifies), 1)
        call = verifies[0]
        self.assertEqual(after(call, "-inform"), "DER")
        self.assertTrue(after(call, "-in").endswith("/dist/SHA256SUMS.p7s"), call.argv)
        self.assertTrue(after(call, "-content").endswith("/dist/SHA256SUMS"), call.argv)
        self.assertIn("-binary", call.argv)
        self.assertEqual(after(call, "-purpose"), "any")
        self.assertEqual(after(call, "-out"), "/dev/null")
        fingerprints = calls_with(result, "openssl", "x509", "-noout", "-fingerprint", "-sha256")
        self.assertEqual(len(fingerprints), 1)
        self.assertEqual(after(fingerprints[0], "-in"), after(call, "-signer"))
        for each in result.calls("openssl"):
            self.assertEqual(each.secret_names(), [], "openssl ran with a credential-like variable")

    def test_a_changed_byte_of_sha256sums_fails_the_verify_and_names_the_signature(self):
        """Task T026, FR-003: SHA256SUMS altered after signing: exit 1, `signature` in the message, no success line, no
        signature_check=ok."""
        tree = self.new_tree()
        tree.prepare_release_files()
        tree.write_certificate()
        sums = tree.work_dir() / "dist" / "SHA256SUMS"
        sums.write_bytes(sums.read_bytes() + b"x")
        result = tree.run("sign-verify")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stderr, r"(?m)^release: .*signature")
        self.assertNotIn("signature check: ok", result.stdout)
        self.assertNotIn("signature_check=ok", recorded(tree))

    def test_a_missing_or_empty_p7s_fails_and_names_the_signature(self):
        """Task T026, FR-003: no SHA256SUMS.p7s, or an empty one, is a `signature` failure, not a pass."""
        for label in ("missing", "empty"):
            with self.subTest(p7s=label):
                tree = self.new_tree()
                tree.prepare_release_files()
                tree.write_certificate()
                p7s = tree.work_dir() / "dist" / "SHA256SUMS.p7s"
                if label == "missing":
                    p7s.unlink()
                else:
                    p7s.write_bytes(b"")
                result = tree.run("sign-verify")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertRegex(result.stderr, r"(?m)^release: .*signature")
                self.assertNotIn("signature_check=ok", recorded(tree))

    def test_a_valid_signature_by_another_certificate_fails_and_names_the_signer(self):
        """Task T026, FR-013, NFR-011: the CMS verify passes but the signer's SHA-256 is not the pinned one: exit 1,
        `signer` in the message, nothing recorded as checked."""
        other = hashlib.sha256(b"another certificate").hexdigest()
        tree, result = self.verified(STUB_SIGNER_SHA256=other)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stderr, r"(?m)^release: .*signer")
        self.assertEqual(len(calls_with(result, "openssl", "cms", "-verify")), 1)
        self.assertNotIn("signature check: ok", result.stdout)
        self.assertNotIn("signer_sha256=", recorded(tree))

    def test_no_certificate_file_stops_the_check(self):
        """Task T026, D-059 item 5: jenkins/release-certificate.json is mandatory; without it there is nothing to compare
        the signer with: exit 1 and the message names the certificate."""
        _, result = self.verified(certificate=False)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("certificate", result.stderr.lower())
        self.assertNotIn("signature check: ok", result.stdout)

    @unittest.skipUnless(shutil.which("openssl"), "openssl is not on PATH: the real round trip cannot run")
    def test_a_real_cms_round_trip_on_a_certificate_generated_now(self):
        """Task T026, FR-003, D-059: with the real openssl, a throwaway code-signing certificate generated now signs
        SHA256SUMS as a detached DER CMS; sign-verify passes with its SHA-256 pinned (SSL_CERT_FILE trusts it as the
        chain's root, as the system bundle trusts Certum's), fails after a changed byte, and fails on another pin. No key
        material is committed."""
        tree = self.new_tree()
        tree.prepare_release_files(signature=False)
        (tree.stubs / "openssl").unlink()
        cert = real_signing_certificate(self, tree.tmp)
        sums = tree.work_dir() / "dist" / "SHA256SUMS"
        made = subprocess.run(["openssl", "cms", "-sign", "-binary", "-in", str(sums), "-signer", str(cert.crt),
                               "-inkey", str(cert.key), "-outform", "DER", "-out", str(sums) + ".p7s", "-md", "sha256"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(made.returncode, 0, made.stderr)
        trust = {"SSL_CERT_FILE": cert.crt.as_posix()}
        tree.write_certificate(cert.sha256)
        good = tree.run("sign-verify", env=trust)
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn(f"signature check: ok SHA256SUMS signer {cert.sha256}", good.stdout)
        tree.write_certificate(hashlib.sha256(b"not this one").hexdigest())
        wrong = tree.run("sign-verify", env=trust)
        self.assertEqual(wrong.returncode, 1, wrong.stdout)
        self.assertRegex(wrong.stderr, r"(?m)^release: .*signer")
        tree.write_certificate(cert.sha256)
        sums.write_bytes(sums.read_bytes() + b"x")
        changed = tree.run("sign-verify", env=trust)
        self.assertEqual(changed.returncode, 1, changed.stdout)
        self.assertRegex(changed.stderr, r"(?m)^release: .*signature")


class SigningMaterial:
    """A throwaway code-signing certificate and key made by the real openssl at test time."""

    def __init__(self, key: Path, crt: Path, sha256: str):
        self.key, self.crt, self.sha256 = key, crt, sha256


def real_signing_certificate(case: unittest.TestCase, folder: Path) -> SigningMaterial:
    key, crt = folder / "throwaway.key", folder / "throwaway.crt"
    made = subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                           "-nodes", "-keyout", str(key), "-out", str(crt), "-subj", "/CN=Fixture Publisher",
                           "-days", "2", "-addext", "extendedKeyUsage=codeSigning"],
                          capture_output=True, text=True, timeout=60)
    case.assertEqual(made.returncode, 0, made.stderr)
    shown = subprocess.run(["openssl", "x509", "-in", str(crt), "-noout", "-fingerprint", "-sha256"],
                           capture_output=True, text=True, timeout=60)
    case.assertEqual(shown.returncode, 0, shown.stderr)
    return SigningMaterial(key, crt, shown.stdout.strip().partition("=")[2].replace(":", "").lower())


@requires_release
class ReleaseTimestamp(ReleaseCase):
    """Task T027 (D-059 item 2, US2-AS1, NFR-002): `timestamp` asks the RFC 3161 server RELEASE_TSA_URL (default
    https://time.certum.pl; https or loopback http only, validated like the other URLs) for a token over
    dist/SHA256SUMS.p7s: `openssl ts -query -data <p7s> -sha256 -cert -out <tmp>.tsq`, then `curl -sS --fail --max-time 60
    -H 'Content-Type: application/timestamp-query' --data-binary @<tmp>.tsq <url> -o dist/SHA256SUMS.p7s.tsr`, then
    `openssl ts -verify -data <p7s> -in <tsr>` with `-CAfile $RELEASE_TSA_CAFILE` when that variable is set. Success
    prints `timestamp: ok` and records timestamp=ok; any failure exits 1 and its message names `timestamp`.
    The fake server of the fixture answers at /tsa; the openssl stub verifies the token against the p7s."""

    def setUp(self):
        super().setUp()
        self.fake = FakeGitHub().start()
        self.addCleanup(self.fake.stop)
        self.tree.prepare_release_files(signature=False)
        self.tree.write_signature(timestamp=False)
        self.url = self.fake.base + "/tsa"

    def stamp(self, env: dict | None = None, tree=None, flags=()):
        return (tree or self.tree).run("timestamp", env={"RELEASE_TSA_URL": self.url, **(env or {})}, bash_flags=flags)

    def tsr(self, tree=None) -> Path:
        return (tree or self.tree).work_dir() / "dist" / "SHA256SUMS.p7s.tsr"

    def test_a_good_token_is_saved_verified_printed_and_recorded(self):
        """Task T027, D-059: the token for the p7s lands in dist/SHA256SUMS.p7s.tsr, `timestamp: ok` is printed and
        timestamp=ok recorded; the server received one POST of content type application/timestamp-query, with no
        Authorization header."""
        result = self.stamp()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"(?m)^timestamp: ok$")
        self.assertRegex(recorded(self.tree), r"(?m)^timestamp=ok$")
        p7s = (self.tree.work_dir() / "dist" / "SHA256SUMS.p7s").read_bytes()
        self.assertEqual(self.tsr().read_bytes(), fake_tsr(p7s))
        posts = [r for r in self.fake.requests if r["path"] == "/tsa"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["method"], "POST")
        self.assertEqual(posts[0]["headers"].get("content-type"), "application/timestamp-query")
        self.assertNotIn("authorization", posts[0]["headers"])

    def test_the_query_covers_the_p7s_and_the_request_is_a_fail_fast_post_of_the_query_file(self):
        """Task T027, D-059: `openssl ts -query -data <p7s> -sha256 -cert -out <file>`, a curl with --fail, --max-time 60,
        the timestamp-query content type, --data-binary @<that file>, the URL and -o <tsr>; then `openssl ts -verify
        -data <p7s> -in <tsr>`, with no -CAfile while RELEASE_TSA_CAFILE is unset; no tool sees a credential-like
        variable."""
        result = self.stamp()
        self.assertEqual(result.returncode, 0, result.stderr)
        queries = calls_with(result, "openssl", "ts", "-query")
        self.assertEqual(len(queries), 1)
        self.assertTrue(after(queries[0], "-data").endswith("/dist/SHA256SUMS.p7s"), queries[0].argv)
        for word in ("-sha256", "-cert"):
            self.assertIn(word, queries[0].argv)
        posts = calls_with(result, "curl", "--fail", "--data-binary")
        self.assertEqual(len(posts), 1)
        argv = posts[0].argv
        self.assertEqual(after(posts[0], "--max-time"), "60")
        self.assertIn("Content-Type: application/timestamp-query", argv)
        self.assertTrue(after(posts[0], "--data-binary").startswith("@"), argv)
        self.assertEqual(after(posts[0], "--data-binary")[1:].rsplit("/", 1)[-1], after(queries[0], "-out").rsplit("/", 1)[-1])
        self.assertIn(self.url, argv)
        self.assertTrue(after(posts[0], "-o").endswith("/dist/SHA256SUMS.p7s.tsr"), argv)
        verifies = calls_with(result, "openssl", "ts", "-verify")
        self.assertEqual(len(verifies), 1)
        self.assertTrue(after(verifies[0], "-data").endswith("/dist/SHA256SUMS.p7s"), verifies[0].argv)
        self.assertTrue(after(verifies[0], "-in").endswith("/dist/SHA256SUMS.p7s.tsr"), verifies[0].argv)
        self.assertNotIn("-CAfile", verifies[0].argv)
        for call in result.calls("openssl") + result.calls("curl"):
            self.assertEqual(call.secret_names(), [], "a tool ran with a credential-like variable")

    def test_a_ca_file_variable_is_given_to_the_verify(self):
        """Task T027, D-059: RELEASE_TSA_CAFILE names the CA bundle of the timestamp chain; the verify gets it as -CAfile."""
        ca = self.tree.tmp / "tsa-ca.pem"
        write(ca, "fixture bundle, not a certificate\n")
        result = self.stamp(env={"RELEASE_TSA_CAFILE": ca.as_posix()})
        self.assertEqual(result.returncode, 0, result.stderr)
        verifies = calls_with(result, "openssl", "ts", "-verify")
        self.assertEqual(len(verifies), 1)
        self.assertTrue(after(verifies[0], "-CAfile").endswith("/tsa-ca.pem"), verifies[0].argv)

    def test_the_default_server_is_the_public_certum_timestamp_server(self):
        """Task T027, D-059: with RELEASE_TSA_URL unset the request goes to https://time.certum.pl (a stub curl answers,
        the test is offline)."""
        write(self.tree.stubs / "curl", TSA_CURL_STUB, executable=True)
        result = self.tree.run("timestamp")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls_with(result, "curl", "https://time.certum.pl")), 1)

    def test_a_url_that_is_neither_https_nor_loopback_is_refused_before_any_request(self):
        """Task T027, NFR-001: another scheme, a host that only starts like a safe one, a userinfo or an option-shaped
        value stops the command naming RELEASE_TSA_URL; curl and openssl do not run."""
        for url in ("http://time.example.test/tsa", "ftp://127.0.0.1/tsa", "https://time.certum.pl@evil.test",
                    "http://127.0.0.1.evil.test/tsa", "file:///etc/passwd", "https://", "--output=x"):
            with self.subTest(url=url):
                tree = self.new_tree()
                tree.prepare_release_files(signature=False)
                tree.write_signature(timestamp=False)
                result = self.stamp(env={"RELEASE_TSA_URL": url}, tree=tree)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn("RELEASE_TSA_URL", result.stderr)
                self.assertEqual(result.calls("curl"), [])
                self.assertEqual(calls_with(result, "openssl", "ts"), [])

    def test_a_server_that_refuses_fails_naming_the_timestamp_and_nothing_is_verified_or_recorded(self):
        """Task T027, D-059: a 503 from the server: exit 1, `timestamp` in the message, no verify, no success."""
        self.fake.tsa_status = 503
        result = self.stamp()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stderr, r"(?mi)^release: .*timestamp")
        self.assertEqual(calls_with(result, "openssl", "ts", "-verify"), [])
        self.assertNotIn("timestamp: ok", result.stdout)
        self.assertNotIn("timestamp=ok", recorded(self.tree))

    def test_a_token_that_does_not_verify_fails_naming_the_timestamp(self):
        """Task T027, D-059: the answer came but `openssl ts -verify` refuses it: exit 1, no success, nothing recorded."""
        tree = self.new_tree(STUB_TS_VERIFY_FAIL="1")
        tree.prepare_release_files(signature=False)
        tree.write_signature(timestamp=False)
        result = self.stamp(tree=tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stderr, r"(?mi)^release: .*timestamp")
        self.assertEqual(len(calls_with(result, "openssl", "ts", "-verify")), 1)
        self.assertNotIn("timestamp: ok", result.stdout)
        self.assertNotIn("timestamp=ok", recorded(tree))

    def test_a_query_openssl_cannot_make_stops_before_the_request(self):
        """Task T027, D-059: `openssl ts -query` fails: exit 1 naming the timestamp, no request is sent."""
        tree = self.new_tree(STUB_TS_QUERY_FAIL="1")
        tree.prepare_release_files(signature=False)
        tree.write_signature(timestamp=False)
        result = self.stamp(tree=tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stderr, r"(?mi)^release: .*timestamp")
        self.assertEqual(result.calls("curl"), [])

    def test_without_a_p7s_there_is_nothing_to_timestamp(self):
        """Task T027, D-059: no dist/SHA256SUMS.p7s: exit 1 naming it, the server is not asked."""
        (self.tree.work_dir() / "dist" / "SHA256SUMS.p7s").unlink()
        result = self.stamp()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("SHA256SUMS.p7s", result.stderr)
        self.assertEqual([r for r in self.fake.requests if r["path"] == "/tsa"], [])





@requires_release
class ReleaseChecks(ReleaseCase):
    """Task T028 (D-059): the checks before the approval (US2-AS4, AS16, FR-005, FR-015). A fault prints `fail <kind>
    <detail>` on standard error (kinds count, name, file, hash, signature, signer, license); every fault is listed, then
    exit 1; a pass prints `ok count 10`, `ok hash <name>` for each file, `ok signature SHA256SUMS`, `ok license LICENSE`
    and `ok license NOTICE`. The files are dist/ (ten files, SHA256SUMS, SHA256SUMS.p7s, SHA256SUMS.p7s.tsr), src/LICENSE
    and src/NOTICE of the work directory; the signature check is the one of `sign-verify` (CMS verify, then the signer
    pinned in jenkins/release-certificate.json), a signer that is not the pinned one is a `signer` fault, a missing p7s,
    tsr or certificate file is stopped and named. `checks` does not verify the timestamp (the `timestamp` command does)."""

    def good(self):
        tree = self.new_tree()
        tree.prepare_release_files()
        tree.write_certificate()
        return tree

    def checks(self, tree):
        return tree.run("checks")

    def resigned(self, tree) -> None:
        """SHA256SUMS rebuilt over what dist/ holds now and signed again (stub signature format)."""
        dist = tree.work_dir() / "dist"
        names = sorted(p.name for p in dist.glob("venom.*"))
        sums = "".join(f"{hashlib.sha256((dist / n).read_bytes()).hexdigest()}  {n}\n" for n in names)
        (dist / "SHA256SUMS").write_text(sums, encoding="utf-8", newline="\n")
        tree.write_signature()

    def test_a_missing_executable_fails_and_names_it(self):
        """Task T028, US2-AS4: a file gone from dist/ is a `file` fault that names it."""
        tree = self.good()
        (tree.work_dir() / "dist" / "venom.linux-386").unlink()
        result = self.checks(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(fault(result, "file", "venom.linux-386"), result.stderr)

    def test_a_hash_that_differs_fails_and_names_the_file(self):
        """Task T028, US2-AS4, FR-005: only the changed file is named."""
        tree = self.good()
        (tree.work_dir() / "dist" / "venom.linux-arm").write_bytes(b"not what was hashed\n")
        result = self.checks(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(fault(result, "hash", "venom.linux-arm"), result.stderr)
        self.assertFalse(fault(result, "hash", "venom.linux-386"), "an unchanged file was named")

    def test_a_signature_that_does_not_verify_fails(self):
        """Task T028, US2-AS4, FR-005, D-059: a p7s that is not SHA256SUMS's is a `signature` fault, from one CMS verify."""
        tree = self.good()
        (tree.work_dir() / "dist" / "SHA256SUMS.p7s").write_bytes(b"cms 0000")
        result = self.checks(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(fault(result, "signature", "SHA256SUMS"), result.stderr)
        self.assertEqual(len(calls_with(result, "openssl", "cms", "-verify")), 1)

    def test_a_valid_signature_by_another_certificate_is_a_signer_fault(self):
        """Task T028, D-059, FR-013: the CMS verify passes but the signer is not the pinned certificate: `fail signer`."""
        tree = self.new_tree(STUB_SIGNER_SHA256=hashlib.sha256(b"another certificate").hexdigest())
        tree.prepare_release_files()
        tree.write_certificate()
        result = self.checks(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertRegex(result.stderr, r"(?m)^fail signer\b")
        self.assertFalse(fault(result, "signature", "SHA256SUMS"), "the signature itself was reported as bad")

    def test_a_missing_p7s_or_timestamp_token_fails_and_names_that_file(self):
        """Task T028, D-059, FR-004: the release carries SHA256SUMS.p7s and SHA256SUMS.p7s.tsr; either one gone from
        dist/ is a `file` fault naming it and not the other."""
        for gone, kept in (("SHA256SUMS.p7s", "SHA256SUMS.p7s.tsr"), ("SHA256SUMS.p7s.tsr", "SHA256SUMS.p7s")):
            with self.subTest(missing=gone):
                tree = self.good()
                (tree.work_dir() / "dist" / gone).unlink()
                result = self.checks(tree)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertRegex(result.stderr, rf"(?m)^fail file {re.escape(gone)}(?![\w.-])")
                self.assertNotRegex(result.stderr, rf"(?m)^fail file {re.escape(kept)}(?![\w.-])")

    def test_no_certificate_file_stops_the_checks_and_names_it(self):
        """Task T028, D-059 item 5: without jenkins/release-certificate.json there is no pinned signer: exit 1, named."""
        tree = self.good()
        (tree.root / "jenkins" / "release-certificate.json").unlink()
        result = self.checks(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("certificate", result.stderr.lower())

    def test_nine_or_eleven_files_stop_the_run(self):
        """Task T028, US2-AS16, FR-015: the count is a fault of its own, with SHA256SUMS and the signature consistent."""
        for found, change in ((9, "remove"), (11, "add")):
            with self.subTest(found=found):
                tree = self.good()
                dist = tree.work_dir() / "dist"
                if change == "remove":
                    (dist / "venom.linux-386").unlink()
                else:
                    (dist / "venom.linux-mips").write_bytes(b"an eleventh file\n")
                self.resigned(tree)
                result = self.checks(tree)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertTrue(fault(result, "count", f"expected 10 executables, found {found}"), result.stderr)
                self.assertFalse(fault(result, "signature", "SHA256SUMS"))

    def test_an_unexpected_name_stops_the_run(self):
        """Task T028, US2-AS16, FR-015: ten files, one of them not on the expected list, is a `name` fault."""
        tree = self.good()
        dist = tree.work_dir() / "dist"
        (dist / "venom.linux-arm").rename(dist / "venom.linux-mips")
        self.resigned(tree)
        result = self.checks(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(fault(result, "name", "venom.linux-mips"), result.stderr)
        self.assertFalse(fault(result, "signature", "SHA256SUMS"))

    def test_a_missing_licence_file_fails(self):
        """Task T028, NFR-008: no LICENSE in the clone at the approved commit is a `license` fault."""
        tree = self.good()
        (tree.work_dir() / "src" / "LICENSE").unlink()
        result = self.checks(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(fault(result, "license", "LICENSE"), result.stderr)

    def test_a_missing_notice_file_fails_like_a_missing_licence(self):
        """Task T079, NFR-008: no NOTICE in the clone at the approved commit is a `license` fault that names it, and the
        LICENSE that is there is not named."""
        tree = self.good()
        (tree.work_dir() / "src" / "NOTICE").unlink()
        result = self.checks(tree)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertTrue(fault(result, "license", "NOTICE"), result.stderr)
        self.assertFalse(fault(result, "license", "LICENSE"), "the present LICENSE was named")

    def test_all_checks_pass_on_the_good_fixture_and_each_check_is_listed(self):
        """Task T028, T079, US2-AS4: status 0 and one `ok` line per check, the ten hashes, LICENSE and NOTICE included."""
        result = self.checks(self.good())
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        for expected in ["ok count 10", "ok signature SHA256SUMS", "ok license LICENSE", "ok license NOTICE",
                         *[f"ok hash {name}" for name in EXPECTED_NAMES]]:
            self.assertIn(expected, lines)
        self.assertEqual(result.stderr.count("fail "), 0)


@requires_release
class ReleaseApprover(ReleaseCase):
    """Task T029: check-approver <name> against RELEASE_APPROVERS, a comma-separated list; exact match, like the
    library's approverAllowed (US2-AS14, NFR-002). Status 0 allowed, 1 refused with the reason on standard error."""

    def check(self, name: str, approvers: str = APPROVERS):
        return self.tree.run("check-approver", [name], env={"RELEASE_APPROVERS": approvers})

    def test_check_approver_accepts_a_name_in_the_comma_separated_list(self):
        """Task T029, US2-AS14: each listed name is accepted."""
        for name in ("maker-example", "second.user"):
            with self.subTest(name=name):
                self.assertEqual(self.check(name).returncode, 0)

    def test_it_refuses_another_user_an_empty_name_the_text_null_and_a_character_outside_the_set(self):
        """Task T029, NFR-002: another user, empty, `null` (a missing submitter), a space, `;`, `/`, an empty list."""
        refused = [("someone-else", APPROVERS), ("", APPROVERS), ("null", APPROVERS), ("null", "maker-example,null"),
                   ("a b", "a b"), ("x;y", "x;y"), ("a/b", "a/b"), ("maker-example", "")]
        for name, approvers in refused:
            with self.subTest(name=name, approvers=approvers):
                result = self.check(name, approvers)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertTrue(result.stderr.strip(), "a refusal printed no reason")
        self.assertEqual(self.tree.run("check-approver", env={"RELEASE_APPROVERS": APPROVERS}).returncode, 1)

    def test_a_prefix_or_a_case_variant_of_an_allowed_name_is_refused(self):
        """Task T029, NFR-002: exact match only: a prefix, an extension and another case are other users."""
        for name in ("maker", "maker-exampl", "maker-example2", "Maker-Example", "MAKER-EXAMPLE", "second", "user"):
            with self.subTest(name=name):
                self.assertEqual(self.check(name).returncode, 1)


RECORD_INPUT = {"agent_label": "docker-e2e", "agent_image": "example/agent:1", "go_version": "go1.27.3",
                "signature_check": "ok", "signer_sha256": SIGNER_SHA256, "timestamp": "ok",
                "windows_mode": "no-certificate", "previous_latest": "none"}


@requires_release
class ReleaseRecord(ReleaseCase):
    """Task T036: `record` writes <work>/release-record.md (US2-AS10, US1-AS10, FR-009, NFR-011).
    Environment: RELEASE_STATUS (success, failed or aborted), RELEASE_TAG, RELEASE_SHA, RELEASE_APPROVER,
    RELEASE_PRIVATE_COMMIT, RELEASE_TIME_LIMIT_MINUTES (default 60). The key=value lines of record-input it reads:
    agent_label, agent_image, signature_check, signer_sha256, timestamp, windows_mode (no-certificate or
    unsigned-choice), image_digest; other keys are ignored. The ten hashes come from dist/SHA256SUMS. The body is a list
    of `- <Label>: <value>` lines (Run status, Tag, Commit, Approver, Signature check, Signer SHA-256, Timestamp, Agent,
    Docker access, Time limit, Windows, Private commit, Image); an input that is missing prints `not recorded`; status 0
    whatever is missing, 1 for an unknown status. D-059: `Key fingerprint` and every word about minisign are gone."""

    def record(self, status: str = "success", tree=None, dist: bool = True, env: dict | None = None, **inputs):
        tree = tree or self.tree
        if dist:
            tree.prepare_release_files()
        tree.write_record_input(**{**RECORD_INPUT, **inputs})
        full = {"RELEASE_STATUS": status, "RELEASE_SHA": tree.sha_tip, "RELEASE_APPROVER": "maker-example",
                "RELEASE_TIME_LIMIT_MINUTES": "45", "RELEASE_PRIVATE_COMMIT": "c" * 40, **(env or {})}
        result = tree.run("record", env=full)
        return result, (tree.work_dir() / "release-record.md")

    def text(self, **kwargs) -> str:
        result, path = self.record(**kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return path.read_text(encoding="utf-8")

    def test_a_successful_run_record_holds_tag_commit_ten_hashes_signature_check_signer_timestamp_and_approver(self):
        """Task T036, US2-AS10, FR-009, NFR-011, D-059: every fact of a good run is in the record; the signer replaces
        the key fingerprint and no word of the record is about minisign."""
        text = self.text()
        self.assertEqual(field(text, "Run status"), "success")
        self.assertEqual(field(text, "Tag"), TAG)
        self.assertEqual(field(text, "Commit"), self.tree.sha_tip)
        self.assertIn("ok", field(text, "Signature check"))
        self.assertEqual(field(text, "Signer SHA-256"), SIGNER_SHA256)
        self.assertEqual(field(text, "Timestamp"), "ok")
        self.assertIsNone(field(text, "Key fingerprint"))
        self.assertNotIn("minisign", text.lower())
        self.assertEqual(field(text, "Approver"), "maker-example")
        sums = (self.tree.work_dir() / "dist" / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(sums), 10)
        for line in sums:
            digest, _, name = line.partition("  ")
            with self.subTest(name=name):
                self.assertTrue(any(name in row and digest in row for row in text.splitlines()), name)

    def test_a_failed_and_an_aborted_run_also_leave_a_record_with_the_status(self):
        """Task T036, US2-AS10: no dist/, no approver, no signature: the record still exists and says the status."""
        for status in ("failed", "aborted"):
            with self.subTest(status=status):
                tree = self.new_tree()
                result, path = self.record(status, tree=tree, dist=False, env={"RELEASE_APPROVER": ""})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(field(path.read_text(encoding="utf-8"), "Run status"), status)
        bad = self.record("weird", tree=self.new_tree())[0]
        self.assertEqual(bad.returncode, 1, "an unknown status was accepted")

    def test_signature_signer_and_timestamp_say_not_recorded_when_the_steps_did_not_run(self):
        """Task T036, D-059: a run that stopped before the signature has no facts to claim: the three lines say so."""
        tree = self.new_tree()
        tree.write_record_input(agent_label="docker-e2e", agent_image="example/agent:1")
        result = tree.run("record", env={"RELEASE_STATUS": "failed"})
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (tree.work_dir() / "release-record.md").read_text(encoding="utf-8")
        for label in ("Signature check", "Signer SHA-256", "Timestamp"):
            with self.subTest(label=label):
                self.assertEqual(field(text, label), "not recorded")

    def test_the_record_names_the_agent_its_docker_access_and_the_time_limit(self):
        """Task T036, US1-AS10, NFR-009: the agent label and image, the shared rootless socket, the limit in minutes."""
        text = self.text()
        self.assertIn("docker-e2e", field(text, "Agent"))
        self.assertIn("example/agent:1", field(text, "Agent"))
        self.assertRegex(field(text, "Docker access"), r"(?i)rootless")
        self.assertIn("45 minutes", field(text, "Time limit"))

    def test_the_record_says_unsigned_by_decision_d_008_while_no_certificate_is_registered(self):
        """Task T036, US3-AS7, D-008: no certificate registered gives the decision wording."""
        self.assertIn("unsigned by decision D-008", field(self.text(), "Windows"))

    def test_the_record_says_unsigned_by_the_makers_choice_at_the_pause_when_a_certificate_exists(self):
        """Task T036, US3-AS6: a registered certificate and an unsigned release is the maker's choice, not D-008."""
        windows = field(self.text(windows_mode="unsigned-choice"), "Windows")
        self.assertIn("unsigned by the maker's choice at the pause", windows)
        self.assertNotIn("D-008", windows)

    def test_the_record_names_the_private_commit_of_the_scripts(self):
        """Task T036, F5: the private commit the scripts came from is in the record."""
        self.assertEqual(field(self.text(), "Private commit"), "c" * 40)

    def test_the_record_says_image_not_published_when_there_is_no_digest(self):
        """Task T036, US4-AS8: no image digest says `image not published`; a digest is shown instead."""
        self.assertEqual(field(self.text(), "Image"), "image not published")
        digest = "sha256:" + "d" * 64
        shown = field(self.text(tree=self.new_tree(), image_digest=digest), "Image")
        self.assertIn(digest, shown)
        self.assertNotIn("not published", shown)

    def test_the_record_has_the_document_header_and_no_credential(self):
        """Task T036, NFR-001: the documentation.md header rows; planted credentials in the environment, on standard
        input and in an unknown record-input key never reach the record or its output."""
        values = [canary("t", "11"), canary("p", "12"), canary("k", "13")]
        tree = self.new_tree()
        tree.prepare_release_files()
        tree.write_record_input(**{**RECORD_INPUT, "secret_note": values[2]})
        env = {"RELEASE_STATUS": "success", "RELEASE_SHA": tree.sha_tip, "PLANTED_TOKEN": values[0],
               "RELEASE_KEY_FILE": values[1]}
        result = tree.run("record", env=env, stdin=values[1] + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        text = (tree.work_dir() / "release-record.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# "), "no title line")
        for row in ("Classification | Restricted", "Status |", "Owner |", r"Work items \| .*#\d+", "Controlled copy |",
                    r"Last updated \| \d{4}-\d{2}-\d{2}"):
            self.assertRegex(text, rf"(?m)^\| {row}")
        for value in values:
            self.assertNotIn(value, text + result.output())
        self.assertNotRegex(text, r"(?i)bearer |-----BEGIN")


@requires_release
class ReleaseSecrets(ReleaseCase):
    """Task T037: the steps commands never print a credential (US2-AS5, NFR-001, NFR-013)."""

    def setUp(self):
        super().setUp()
        self.fake = FakeGitHub().start()
        self.addCleanup(self.fake.stop)

    def setup_sign_verify(self, tree) -> dict:
        tree.prepare_release_files()
        tree.write_certificate()
        return {}

    def setup_timestamp(self, tree) -> dict:
        tree.prepare_release_files(signature=False)
        tree.write_signature(timestamp=False)
        return {"RELEASE_TSA_URL": self.fake.base + "/tsa"}

    def setup_build(self, tree) -> dict:
        return {}

    def test_each_command_run_under_bash_x_with_a_canary_on_standard_input_prints_no_canary(self):
        """Task T037, US2-AS5, NFR-001, D-059: sign-verify, timestamp and build under `bash -x`, a canary on standard
        input and planted variables: no canary in either output stream or in any file the run left in the tree."""
        stdin_canary = canary("stdin", "21")
        values = [*PLANTED.values(), stdin_canary]
        for command in ("sign-verify", "timestamp", "build"):
            with self.subTest(command=command):
                tree = self.new_tree()
                env = {**PLANTED, **getattr(self, "setup_" + command.replace("-", "_"))(tree)}
                result = tree.run(command, env=env, stdin=stdin_canary + "\n", bash_flags=("-x",))
                self.assertEqual(result.returncode, 0, result.stderr[-500:])
                for value in values:
                    self.assertNotIn(value, result.output())
                    self.assertEqual(files_holding([tree.work_root, tree.root], value), [])

    def test_the_build_and_checks_commands_run_with_no_credential_variable_set(self):
        """Task T037, NFR-013: with no key file, password, token or registry variable, build, sums and checks pass,
        and the tools they run see no credential-like variable."""
        tree = self.new_tree()
        for command in ("build", "sums"):
            self.assertEqual(tree.run(command).returncode, 0, command)
        signed = self.new_tree()
        signed.prepare_release_files()
        signed.write_certificate()
        result = signed.run("checks")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = result.calls("openssl")
        self.assertTrue(calls, "checks did not verify the signature")
        for call in calls:
            self.assertEqual(call.secret_names(), [])


# ---- US3: the Windows pause (tasks T053, T054) ----

CERT_FIELDS = {"sha256": SIGNER_SHA256,
               "subject": "CN=Fixture Publisher",
               "validUntil": (datetime.date.today() + datetime.timedelta(days=400)).isoformat()}
WINDOWS_EXE = "venom.windows-amd64.exe"


def sums_map(text: str) -> dict:
    """File name to hash of a SHA256SUMS text."""
    return {line.split("  ")[1]: line.split("  ")[0] for line in text.splitlines()}


@requires_release
class ReleaseWindows(ReleaseCase):
    """Tasks T053 and T054: the Windows pause commands (US3, FR-007). The certificate file is
    <tree>/jenkins/release-certificate.json, a JSON object with sha256 (64 lowercase hex), subject (non-empty text)
    and validUntil (a YYYY-MM-DD date not in the past). `windows-mode [sign|unsigned]`: no certificate file prints
    `windows mode: unsigned`, writes windows_mode=no-certificate to record-input and exits 0, whatever the argument.
    With a certificate file the argument is required: `unsigned` writes windows_mode=unsigned-choice and exits 0
    before reading the file; `sign` checks the file (exit 1, stderr names the faulty field: sha256, subject or
    validUntil, and windows_mode is not written) and with a valid file (D-058) exits 0, prints
    `windows mode: sign (agent)` and writes windows_mode=agent-signed. `windows-verify <report-file>` checks the
    report of the Windows agent (see below). `resign-sums` (no argument) rewrites
    dist/SHA256SUMS over the ten files as they are now, in the format of `sums`, and leaves no SHA256SUMS.p7s and no
    SHA256SUMS.p7s.tsr behind (D-059: a signature or a timestamp of the old sums must not survive); it needs no key and
    no signer tool."""

    def certificate(self, tree, **changes) -> Path:
        fields = {**CERT_FIELDS, **changes}
        body = ",".join(f'"{k}": "{v}"' for k, v in fields.items() if v is not None)
        path = tree.root / "jenkins" / "release-certificate.json"
        write(path, "{" + body + "}\n")
        return path

    def recorded(self, tree) -> str:
        path = tree.make_work() / "record-input"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def record_text(self, tree) -> str:
        tree.prepare_release_files()
        result = tree.run("record", env={"RELEASE_STATUS": "success", "RELEASE_SHA": tree.sha_tip})
        self.assertEqual(result.returncode, 0, result.stderr)
        return (tree.work_dir() / "release-record.md").read_text(encoding="utf-8")

    def signed_release(self):
        tree = self.new_tree()
        assets = tree.prepare_release_files(signature=False)
        dist = tree.work_dir() / "dist"
        (dist / WINDOWS_EXE).write_bytes(b"signed " + assets[WINDOWS_EXE])
        return tree, dist, assets

    def test_no_certificate_file_means_mode_unsigned_and_the_record_says_unsigned_by_decision_d_008(self):
        """Task T053, US3-AS5, D-008: without jenkins/release-certificate.json the mode is unsigned and the record
        names the decision."""
        result = self.tree.run("windows-mode")
        self.assertEqual(result.returncode, 0, result.output())
        self.assertIn("windows mode: unsigned", result.stdout)
        self.assertIn("windows_mode=no-certificate", self.recorded(self.tree))
        self.assertIn("unsigned by decision D-008", field(self.record_text(self.tree), "Windows"))

    def test_a_certificate_file_with_a_bad_sha256_a_missing_subject_or_a_past_valid_until_is_refused(self):
        """Task T053, US3-AS5: `sign` refuses a certificate file that breaks the library's shape and names the
        field; the mode is not recorded."""
        faults = {"sha256": {"sha256": "not-a-hash"}, "subject": {"subject": None},
                  "validUntil": {"validUntil": "2000-01-01"}}
        for field_name, changes in faults.items():
            with self.subTest(field=field_name):
                tree = self.new_tree()
                self.certificate(tree, **changes)
                result = tree.run("windows-mode", ["sign"])
                self.assertEqual(result.returncode, 1, result.output())
                self.assertIn(field_name, result.stderr)
                self.assertNotIn("windows_mode=", self.recorded(tree))

    def test_windows_mode_unsigned_returns_before_any_refusal_even_with_a_valid_certificate_file_and_the_record_says_unsigned_by_the_makers_choice_at_the_pause(self):
        """Task T053, US3-AS6: the maker chose unsigned at the pause; no refusal, and the record says it is the
        maker's choice, not D-008."""
        self.certificate(self.tree)
        result = self.tree.run("windows-mode", ["unsigned"])
        self.assertEqual(result.returncode, 0, result.output())
        self.assertIn("windows_mode=unsigned-choice", self.recorded(self.tree))
        windows = field(self.record_text(self.tree), "Windows")
        self.assertIn("unsigned by the maker's choice at the pause", windows)
        self.assertNotIn("D-008", windows)

    def test_windows_mode_sign_with_a_valid_certificate_file_prints_sign_agent_and_records_agent_signed(self):
        """Task T053, US3-AS5, D-058: `sign` with a valid certificate file no longer refuses (it did while T057 was
        parked): it prints `windows mode: sign (agent)` and records windows_mode=agent-signed."""
        self.certificate(self.tree)
        result = self.tree.run("windows-mode", ["sign"])
        self.assertEqual(result.returncode, 0, result.output())
        self.assertIn("windows mode: sign (agent)", result.stdout)
        self.assertRegex(self.recorded(self.tree), r"(?m)^windows_mode=agent-signed$")

    def test_a_record_after_windows_mode_sign_alone_does_not_claim_the_file_was_signed(self):
        """Task T053, D-058, NFR-002: agent-signed mode without a verified signer is not a claim of signing; only
        windows-verify records the signer."""
        self.certificate(self.tree)
        self.assertEqual(self.tree.run("windows-mode", ["sign"]).returncode, 0)
        self.assertNotIn("signed by the windows-sign agent", field(self.record_text(self.tree), "Windows"))

    # ---- windows-verify <report-file> (D-058, tasks T053, T055, T056) ----
    # The report is text made on the Windows agent: lines Status=, SignerSha256=, Timestamped=, FileSha256= (CRLF or LF),
    # at most 4096 bytes. SignerSha256 is compared with the sha256 field of jenkins/release-certificate.json, FileSha256
    # with the SHA-256 of dist/venom.windows-amd64.exe in the work directory.

    def verify_tree(self):
        tree, dist, _ = self.signed_release()
        self.certificate(tree)
        self.assertEqual(tree.run("windows-mode", ["sign"]).returncode, 0)
        return tree, hashlib.sha256((dist / WINDOWS_EXE).read_bytes()).hexdigest()

    def report(self, tree, file_sha: str, newline: str = "\n", **changes) -> Path:
        lines = {"Status": "Valid", "SignerSha256": CERT_FIELDS["sha256"], "Timestamped": "true", "FileSha256": file_sha}
        lines.update(changes)
        text = newline.join(f"{k}={v}" for k, v in lines.items() if v is not None) + newline
        path = tree.tmp / "windows-report.txt"
        path.write_bytes(text.encode("utf-8"))
        return path

    def test_windows_verify_passes_a_valid_report_and_records_the_signer_and_the_record_names_the_agent_and_the_certificate(self):
        """Task T055, US3-AS3, US3-AS7, D-058: a report with Status=Valid, Timestamped=true, the pinned signer and
        the hash of the signed file passes; record-input gets windows_signer=<sha256> and windows_signed=yes and the
        record says it was signed by the windows-sign agent with that certificate."""
        tree, file_sha = self.verify_tree()
        result = tree.run("windows-verify", [self.report(tree, file_sha).as_posix()])
        self.assertEqual(result.returncode, 0, result.output())
        recorded = self.recorded(tree)
        self.assertRegex(recorded, rf"(?m)^windows_signer={CERT_FIELDS['sha256']}$")
        self.assertRegex(recorded, r"(?m)^windows_signed=yes$")
        self.assertIn(f"signed by the windows-sign agent with the certificate {CERT_FIELDS['sha256']}",
                      field(self.record_text(tree), "Windows"))

    def test_windows_verify_accepts_a_report_with_windows_line_endings(self):
        """Task T055, D-058: PowerShell writes CRLF; the report is read the same."""
        tree, file_sha = self.verify_tree()
        result = tree.run("windows-verify", [self.report(tree, file_sha, newline="\r\n").as_posix()])
        self.assertEqual(result.returncode, 0, result.output())

    def test_windows_verify_refuses_a_failing_field_and_names_it_and_records_nothing(self):
        """Task T055, US3-AS3, D-058: a report that is not Valid, not timestamped, signed by another certificate or
        describing another file exits 1, stderr names status, timestamp, signer or hash, and windows_signed is not
        recorded."""
        other = hashlib.sha256(b"another").hexdigest()
        faults = {"status": {"Status": "NotSigned"}, "timestamp": {"Timestamped": "false"},
                  "signer": {"SignerSha256": other}, "hash": {"FileSha256": other}}
        for word, changes in faults.items():
            with self.subTest(field=word):
                tree, file_sha = self.verify_tree()
                result = tree.run("windows-verify", [self.report(tree, file_sha, **changes).as_posix()])
                self.assertEqual(result.returncode, 1, result.output())
                self.assertIn(word, result.stderr.lower())
                self.assertNotIn("windows_signed=", self.recorded(tree))

    def test_windows_verify_refuses_a_missing_empty_oversized_or_malformed_report(self):
        """Task T055, D-058: no file, an empty file, a file over 4 KB, a line that is not key=value, an unknown key,
        a duplicate key, a missing key or a non-hex hash are refused with exit 1 and nothing recorded."""
        tree, file_sha = self.verify_tree()
        good = self.report(tree, file_sha).read_text(encoding="utf-8")
        cases = {"missing": None, "empty": "", "oversized": good + "\n" * 5000, "no equals": good + "garbage\n",
                 "unknown key": good + "Extra=1\n", "duplicate": good + "Status=Valid\n",
                 "missing key": "".join(good.splitlines(True)[:3]),
                 "bad hex": good.replace(f"FileSha256={file_sha}", "FileSha256=" + file_sha.upper())}
        for label, text in cases.items():
            with self.subTest(report=label):
                path = tree.tmp / f"report-{label.replace(' ', '-')}.txt"
                if text is not None:
                    path.write_bytes(text.encode("utf-8"))
                result = tree.run("windows-verify", [path.as_posix()])
                self.assertEqual(result.returncode, 1, result.output())
                self.assertNotIn("windows_signed=", self.recorded(tree))

    def test_windows_verify_refuses_when_there_is_no_certificate_file_or_no_argument(self):
        """Task T055, D-058: nothing to compare the signer with, or no report named, is a refusal."""
        tree, file_sha = self.verify_tree()
        report = self.report(tree, file_sha)
        self.assertEqual(tree.run("windows-verify").returncode, 1)
        (tree.root / "jenkins" / "release-certificate.json").unlink()
        self.assertEqual(tree.run("windows-verify", [report.as_posix()]).returncode, 1)

    def test_resign_sums_rebuilds_sha256sums_over_the_signed_file_and_the_other_nine_are_unchanged(self):
        """Task T054, US3-AS3, FR-007: after the signed .exe replaces the built one, SHA256SUMS lists the new hash of
        it, the old hash of the other nine, and neither the p7s nor the timestamp token of the old sums stays (D-059)."""
        tree, dist, assets = self.signed_release()
        tree.write_signature()
        old = sums_map(assets["SHA256SUMS"].decode())
        result = tree.run("resign-sums")
        self.assertEqual(result.returncode, 0, result.output())
        new = sums_map((dist / "SHA256SUMS").read_text(encoding="utf-8"))
        self.assertEqual(sorted(new), sorted(EXPECTED_NAMES))
        self.assertEqual(new[WINDOWS_EXE], hashlib.sha256((dist / WINDOWS_EXE).read_bytes()).hexdigest())
        self.assertNotEqual(new[WINDOWS_EXE], old[WINDOWS_EXE])
        self.assertEqual({n: h for n, h in new.items() if n != WINDOWS_EXE},
                         {n: h for n, h in old.items() if n != WINDOWS_EXE})
        for name in ("SHA256SUMS.p7s", "SHA256SUMS.p7s.tsr"):
            self.assertFalse((dist / name).exists(), f"{name} of the old sums was kept")

    def test_the_new_sums_must_be_signed_again_before_checks(self):
        """Task T054, FR-007, D-059: checks fails after resign-sums until the windows-sign agent signs the new sums again
        (the fixture writes that signature); with it, and the timestamp, checks passes."""
        tree, _, _ = self.signed_release()
        tree.write_certificate()
        tree.write_signature()
        self.assertEqual(tree.run("resign-sums").returncode, 0)
        self.assertEqual(tree.run("checks").returncode, 1, "the new sums were not signed yet")
        tree.write_signature()
        checked = tree.run("checks")
        self.assertEqual(checked.returncode, 0, checked.output())


if __name__ == "__main__":
    unittest.main()
