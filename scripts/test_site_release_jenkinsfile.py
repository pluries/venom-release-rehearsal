#!/usr/bin/env python3
"""Text guards of the release job's gate files (feature 011, task T038, lands with T050):
jenkins/release.Jenkinsfile, a declarative pipeline with no library, and jenkins/jobs/venom-release.xml, its job.
These are text tests, not a Jenkins run; the live proof is the maker's (T051, T052). Offline, standard library only.
Run: python -I -c "import sys;sys.path.insert(0,'scripts');import unittest;unittest.main(module='test_site_release_jenkinsfile',argv=['x'],exit=False)"

Contract the Jenkinsfile is written to (design 1 of plan.md):
- the shell helpers rs, rh, ri and rl (defined once in shellPrelude) call release-steps.sh, release-github.sh,
  release-image.sh and release-leak-check.sh from $RELEASE_BIN, the copies made at Resolve; rs, rh and ri run under
  `timeout "$(remaining <deadline>)"`. Every stage calls `rs setup` first.
- credentials are bound with withCredentials, by id, in the stage that needs them.
- the job parameters (RUN_MODE, RELEASE_TAG, RELEASE_NOTES, RELEASE_BASE) are defined by the job XML.
- D-058, D-059: the stages named in WINDOWS_STAGES run on the agent labelled windows-sign with inline steps only, and
  in release mode always: signing is mandatory, minisign is dropped, Certum signs the exe and SHA256SUMS.
"""
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_jenkinsfile import read_repo  # noqa: E402

RELEASE = "jenkins/release.Jenkinsfile"
RELEASE_JOB = "jenkins/jobs/venom-release.xml"
PUBLISH_JOB = "jenkins/jobs/venom-publish.xml"

STAGES = ["Resolve", "Windows preflight", "Claim", "Build", "Windows sign", "Resign sums", "Windows sign sums", "Seal",
          "Checks", "Approve", "Draft and publish", "Image"]
WINDOWS_STAGES = ("Windows preflight", "Windows sign", "Windows sign sums")
RELEASE_ONLY_AGENT_STAGES = ("Claim", "Build", "Resign sums", "Seal", "Checks", "Draft and publish", "Image")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
APPROVER_CHARS = re.compile(r"^[A-Za-z0-9._@-]+$")
SHARED_ACCOUNTS = {"admin", "administrator", "jenkins", "root", "release", "releases", "anonymous", "authenticated",
                   "everyone", "all", "team", "ci", "bot", "devops", "maker"}
TOKEN_ID = "release-github-token"
REGISTRY_ID = "release-registry"
HELPER_CALL = re.compile(r"(?<![\w$])(rs|rh|ri|rl)\s+([a-z][a-z-]*)")


def code_of(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^[ \t]*//.*$", "", text)


def match_brace(text: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return i
    raise AssertionError("unbalanced braces")


def block_after(text: str, pattern: str):
    m = re.search(pattern, text)
    if not m:
        return None
    open_idx = text.index("{", m.start())
    return text[open_idx + 1:match_brace(text, open_idx)]


def stages_of(code: str) -> list:
    found = []
    for m in re.finditer(r"stage\('([^']+)'\)\s*\{", code):
        end = match_brace(code, m.end() - 1)
        found.append((m.group(1), code[m.end():end], m.start()))
    return found


def stage(code: str, name: str) -> str:
    for n, body, _ in stages_of(code):
        if n == name:
            return body
    raise AssertionError(f"no stage named {name}")


def post_block(code: str) -> str:
    m = re.search(r"\n  post\s*\{", code)
    assert m, "no pipeline-level post block"
    return code[m.end():match_brace(code, m.end() - 1)]


def helper_calls(text: str) -> list:
    return [(m.group(1), m.group(2), m.start()) for m in HELPER_CALL.finditer(text)]


def constant(code: str, name: str):
    m = re.search(rf"(?m)^[ \t]*{name}\s*=\s*'([^']*)'", code)
    return m.group(1) if m else None


class ReleaseJenkinsfile(unittest.TestCase):
    """T038: the shape of jenkins/release.Jenkinsfile."""

    @classmethod
    def setUpClass(cls):
        cls.text = read_repo(RELEASE)
        cls.code = code_of(cls.text)
        cls.stages = stages_of(cls.code)

    def test_every_backslash_of_the_jenkinsfile_is_doubled_because_groovy_reads_escapes(self):
        """D-058, D-059 (found by the first rehearsal run): Groovy processes backslash escapes in a single-quoted string, so a
        PowerShell path such as Cert:, backslash, CurrentUser must be written with doubled backslashes; one backslash is an
        illegal escape and Jenkins refuses the whole file before any stage runs. Text guards cannot compile Groovy, so every
        backslash must belong to a doubled pair."""
        bs = chr(92)
        bad = []
        for number, line in enumerate(self.text.split(chr(10)), 1):
            if bs in line.replace(bs + bs, ''):
                bad.append((number, line.strip()[:80]))
        self.assertEqual([], bad, "a single backslash in a Groovy string is an illegal escape")

    def test_every_agent_stage_runs_on_docker_e2e_and_waits_hold_no_agent(self):
        code = self.code
        self.assertRegex(code, r"pipeline\s*\{\s*agent\s+none\b")
        agents = re.findall(r"\bagent\b[^\n]*", code)
        for line in agents:
            self.assertRegex(line, r"^agent\s+(none\b|\{\s*label\s+'(docker-e2e|windows-sign)'\s*\})", line)
        for name in WINDOWS_STAGES:
            self.assertRegex(stage(code, name), r"agent\s*\{\s*label\s+'windows-sign'\s*\}", name)
        self.assertEqual(len(WINDOWS_STAGES), len(re.findall(r"label\s+'windows-sign'", code)))
        for node in re.findall(r"\bnode\s*\(([^)]*)\)", code):
            self.assertEqual("'docker-e2e'", node.strip())
        self.assertNotRegex(code, r"\bagent\s+any\b")
        self.assertTrue(re.search(r"\binput\s*\{", stage(code, "Approve")), "Approve has no input directive")
        self.assertNotRegex(stage(code, "Approve"), r"agent\s*\{")
        self.assertRegex(stage(code, "Approve"), r"agent\s+none")
        self.assertRegex(stage(code, "Resolve"), r"agent\s+none")

    def test_the_stage_order_has_the_windows_preflight_before_the_claim_and_the_two_windows_signatures_between_build_and_checks(self):
        """Task T050, D-058, D-059: Resolve, Windows preflight, Claim, Build, Windows sign, Resign sums, Windows sign
        sums, Seal, Checks, Approve, publish, Image; the Sign stage (minisign) is gone and nothing is published
        before Approve."""
        names = [n for n, _, _ in self.stages]
        self.assertEqual(STAGES, names)
        approve = [pos for n, _, pos in self.stages if n == "Approve"][0]
        for n, body, pos in self.stages:
            for helper, command, _ in helper_calls(body):
                if helper == "rh" and command in ("draft", "upload", "verify-draft", "publish", "verify-public", "withdraw"):
                    self.assertGreater(pos, approve, f"{n} calls rh {command} before Approve")
        publish_stage = stage(self.code, "Draft and publish")
        commands = [c for h, c, _ in helper_calls(publish_stage) if h == "rh"]
        self.assertEqual(["draft", "upload", "verify-draft", "publish", "verify-public"], commands)
        for n, body, _ in self.stages:
            if n != "Draft and publish":
                self.assertFalse([c for h, c, _ in helper_calls(body) if h == "rh" and c in ("draft", "upload", "publish")], n)

    def test_there_are_exactly_two_inputs_confirm_tag_and_approve_and_no_windows_pause(self):
        """Task T050, D-058: the Windows pause input sign-windows is gone; confirm-tag and approve are the only inputs."""
        directive = re.findall(r"\binput\s*\{[^}]*?\bid\s+'([^']+)'", self.code, flags=re.S)
        step = re.findall(r"\binput\s*\(\s*id\s*:\s*'([^']+)'", self.code)
        ids = directive + step
        self.assertEqual(["approve"], [i for i in ids if i == "approve"])
        self.assertEqual(["confirm-tag"], [i for i in ids if i == "confirm-tag"])
        self.assertEqual(2, len(ids), ids)
        self.assertNotIn("sign-windows", self.code)
        self.assertNotIn("Windows pause", self.code)
        self.assertEqual(len(re.findall(r"\binput\b\s*[({]", self.code)), len(ids))

    def test_run_mode_image_only_skips_claim_and_the_github_stages_and_runs_fetch_check_tag_build_push_with_no_minisign_step(self):
        """D-059: fetch verifies the CMS signature with openssl itself, so there is no fetch-minisign and no check-key
        in the Image stage, and no credential is bound while fetch runs."""
        release_only = [n for n in STAGES if n not in ("Resolve", "Image")]
        for name in release_only:
            body = stage(self.code, name)
            when = block_after(body, r"\bwhen\s*\{")
            self.assertIsNotNone(when, f"{name} has no when")
            self.assertIn("params.RUN_MODE == 'release'", when, name)
        self.assertIsNone(re.search(r"\bwhen\s*\{", stage(self.code, "Image")))
        image = stage(self.code, "Image")
        calls = [(h, c) for h, c, _ in helper_calls(image)]
        wanted = [("rs", "setup"), ("ri", "fetch"), ("ri", "check-tag"),
                  ("ri", "build"), ("ri", "push"), ("ri", "verify"), ("ri", "latest")]
        for gone in ("fetch-minisign", "check-key", "sign"):
            self.assertNotIn(("rs", gone), calls, gone)
        positions = []
        for item in wanted:
            self.assertIn(item, calls, item)
            positions.append(calls.index(item))
        self.assertEqual(sorted(positions), positions, "the image commands are out of order")
        self.assertEqual("image-only", self.run_mode_choices()[1])
        creds = image.index("withCredentials")
        at = [pos for h, c, pos in helper_calls(image) if (h, c) == ("ri", "fetch")][0]
        self.assertLess(at, creds, "ri fetch runs with a credential bound")
        self.assertNotIn(TOKEN_ID, image)
        self.assertRegex(self.code, r"deadline\s+\"\$RELEASE_START\"\s+3600")
        self.assertIn("params.RELEASE_TAG", stage(self.code, "Resolve"))

    def run_mode_choices(self) -> list:
        root = ET.fromstring(read_repo(RELEASE_JOB))
        for param in root.iter("hudson.model.ChoiceParameterDefinition"):
            if param.findtext("name") == "RUN_MODE":
                return [s.text for s in param.iter("string")]
        raise AssertionError("no RUN_MODE choice parameter")

    def test_resolve_records_the_private_commit_and_copies_the_scripts_and_later_stages_and_post_run_the_copies_without_checkout_scm(self):
        resolve = stage(self.code, "Resolve")
        self.assertEqual(1, len(re.findall(r"\bcheckout\s+scm\b", self.code)))
        self.assertIn("checkout scm", resolve)
        self.assertIn("RELEASE_PRIVATE_COMMIT", resolve)
        self.assertRegex(resolve, r"jenkins/release-\*\.sh")
        self.assertRegex(resolve, r"jenkins/release-\*\.txt")
        self.assertRegex(resolve, r"\"?\$RELEASE_BIN\"?")
        for name, body, _ in self.stages:
            if name != "Resolve":
                self.assertNotIn("jenkins/", body, f"{name} reads the checkout instead of the copies")
        self.assertNotIn("jenkins/", post_block(self.code))
        self.assertRegex(self.code, r"RELEASE_BIN\s*=\s*\"\$\{RELEASE_WORK_DIR\}/bin\"")
        for name, body, _ in self.stages:
            if name == "Resolve" or name in WINDOWS_STAGES:
                continue
            self.assertTrue(helper_calls(body) or "withCredentials" in body or "input" in body, name)
        self.assertIn('release-steps.sh', self.code)

    def test_post_copies_the_record_into_its_own_workspace_before_archive_artifacts_and_finds_claim_flag_by_the_derived_path(self):
        post = post_block(self.code)
        copy_at = post.index('cp "$RELEASE_WORK_DIR"/release-record')
        archive_at = post.index("archiveArtifacts")
        self.assertLess(copy_at, archive_at)
        self.assertIn('"$WORKSPACE"', post[copy_at:archive_at])
        self.assertIn('"$RELEASE_WORK_DIR/claim.flag"', post)
        derived = re.search(r"(?m)^\s*RELEASE_WORK_DIR\s*=\s*(\".*\")\s*$", self.code)
        self.assertTrue(derived)
        for part in ("RELEASE_WORK_ROOT", "JOB_NAME", "BUILD_NUMBER"):
            self.assertIn(part, derived.group(1))
        self.assertNotIn("WORKSPACE", derived.group(1))

    def test_post_runs_in_a_docker_e2e_node_and_cleanup_binds_the_token_only_when_a_tag_was_claimed_and_nothing_published(self):
        post = post_block(self.code)
        self.assertRegex(post, r"node\('docker-e2e'\)\s*\{")
        self.assertEqual(1, len(helper_calls(post)) + len(re.findall(r"release-github\.sh\"\s+cleanup", post)))
        guard = re.search(r"if\s*\(\s*claimed\s*\)\s*\{", post)
        self.assertTrue(guard)
        guarded = post[guard.end():match_brace(post, guard.end() - 1)]
        self.assertIn(TOKEN_ID, guarded)
        self.assertIn("cleanup", guarded)
        self.assertEqual(1, post.count(TOKEN_ID))
        self.assertNotIn("rm -f", post)
        self.assertNotIn("claim.flag\"", self.code.replace('"$RELEASE_WORK_DIR/claim.flag"', ""))

    def test_the_one_approval_input_has_a_24_hour_timeout_a_submitter_and_the_sha_and_tag_in_the_message(self):
        body = stage(self.code, "Approve")
        self.assertEqual(1, len(re.findall(r"timeout\(time:\s*24,\s*unit:\s*'HOURS'\)", self.code)))
        self.assertRegex(body, r"timeout\(time:\s*24,\s*unit:\s*'HOURS'\)")
        inp = block_after(body, r"\binput\s*\{")
        self.assertRegex(inp, r"\bsubmitter\s+\"\$\{env\.RELEASE_APPROVER\}\"")
        self.assertRegex(inp, r"\bsubmitterParameter\s+'APPROVED_BY'")
        message = re.search(r"\bmessage\s+\"([^\"]*)\"", inp).group(1)
        self.assertIn("${env.RELEASE_SHA}", message)
        self.assertIn("${env.RELEASE_TAG}", message)

    def test_the_approver_literal_is_a_single_named_user_matching_the_character_set_and_not_a_shared_account(self):
        approver = constant(self.code, "RELEASE_APPROVER")
        self.assertIsNotNone(approver)
        self.assertRegex(approver, APPROVER_CHARS)
        self.assertNotIn(",", approver)
        self.assertNotIn(approver.lower(), SHARED_ACCOUNTS)
        self.assertEqual(1, len(re.findall(r"(?m)^[ \t]*RELEASE_APPROVER\s*=", self.code)))
        self.assertRegex(self.code, r"RELEASE_APPROVERS\s*=\s*\"\$\{RELEASE_APPROVER\}\"")
        self.assertNotRegex(stage(self.code, "Approve"), r"submitter\s+'")

    def test_the_approver_is_re_checked_in_code_after_the_prompt(self):
        approve_at = [pos for n, _, pos in self.stages if n == "Approve"][0]
        found = []
        for n, body, pos in self.stages:
            for h, c, _ in helper_calls(body):
                if (h, c) == ("rs", "check-approver"):
                    found.append((n, pos))
        self.assertEqual(1, len(found))
        self.assertGreater(found[0][1], approve_at)
        self.assertIn('rs check-approver "$RELEASE_APPROVED_BY"', stage(self.code, "Draft and publish"))
        self.assertIn("env.RELEASE_APPROVED_BY = env.APPROVED_BY", stage(self.code, "Approve"))

    def test_each_credential_is_bound_only_in_its_stage(self):
        where = {}
        for name, body, _ in self.stages:
            for cid in (TOKEN_ID, REGISTRY_ID):
                if cid in body:
                    where.setdefault(cid, set()).add(name)
        self.assertEqual({TOKEN_ID: {"Claim", "Draft and publish"}, REGISTRY_ID: {"Image"}}, where)
        self.assertIn(TOKEN_ID, post_block(self.code))
        for cid in (TOKEN_ID, REGISTRY_ID):
            self.assertRegex(cid, r"^[a-z0-9][a-z0-9-]*$")
        for name in ("Build", "Resign sums", "Seal", "Checks"):
            self.assertNotIn("withCredentials", stage(self.code, name), name)
            self.assertNotIn("credentials", stage(self.code, name).lower(), name)
        self.assertEqual(4, len(re.findall(r"credentialsId\s*:", self.code)), "the token three times, the registry once")
        self.assertEqual(4, len(re.findall(r"\bwithCredentials\s*\(", self.code)), "claim, publish, image, cleanup")

    def test_the_certificate_pin_and_the_timestamp_url_are_constants_and_the_minisign_constants_are_gone(self):
        """D-059: RELEASE_TSA_URL is the Certum timestamp server; no minisign URL, hash or key fingerprint is left."""
        self.assertRegex(constant(self.code, "RELEASE_CERT_SHA256"), HEX64)
        self.assertEqual("https://time.certum.pl", constant(self.code, "RELEASE_TSA_URL"))
        self.assertEqual(1, len(re.findall(r"(?m)^[ \t]*RELEASE_TSA_URL\s*=", self.code)))
        for name in ("RELEASE_MINISIGN_URL", "RELEASE_MINISIGN_SHA256", "RELEASE_KEY_FINGERPRINT", "RELEASE_PUBKEY"):
            self.assertNotIn(name, self.text, name)
        for word in ("minisign", "usemini", "fetch-minisign", "check-key"):
            self.assertNotIn(word, self.text.lower(), word)
        self.assertNotIn(("rs", "sign"), [(h, c) for h, c, _ in helper_calls(self.code)])
        for name in ("RELEASE_PUBLIC_URL", "RELEASE_REPOSITORY", "RELEASE_IMAGE", "RELEASE_WORK_ROOT"):
            self.assertTrue(constant(self.code, name), name)
        self.assertTrue(constant(self.code, "RELEASE_WORK_ROOT").startswith("/"))
        self.assertNotRegex(self.code, r"(?i)(password|token|secret)\s*=\s*'[^']+'")

    def test_no_library_is_loaded_and_no_im_step_is_used(self):
        self.assertNotIn("@Library", self.text)
        self.assertNotRegex(self.code, r"\blibrary\s*[('\"]")
        self.assertNotRegex(self.code, r"\bim[A-Z][A-Za-z]*\s*\(")
        self.assertNotIn("sdlc@", self.code)

    def test_the_job_xml_has_exactly_one_main_branch_spec_and_no_parameter_named_branch_and_the_jenkinsfile_stops_when_the_checked_out_commit_is_not_origin_main(self):
        root = ET.fromstring(read_repo(RELEASE_JOB))
        branches = [spec.findtext("name") for spec in root.iter("hudson.plugins.git.BranchSpec")]
        self.assertEqual(["*/main"], branches)
        params = [p.findtext("name") for p in root.find("properties/hudson.model.ParametersDefinitionProperty/parameterDefinitions")]
        self.assertNotIn("BRANCH", params)
        self.assertNotRegex(self.code, r"\b(BRANCH_NAME|CHANGE_ID)\b")
        resolve = stage(self.code, "Resolve")
        self.assertRegex(resolve, r"git rev-parse origin/main")
        self.assertRegex(resolve, r"test \"\$RELEASE_PRIVATE_COMMIT\" = \"\$\(git rev-parse origin/main\)\"")
        self.assertIn("scmVars.GIT_COMMIT", resolve)

    def test_the_agent_stages_run_setup_first(self):
        for name in RELEASE_ONLY_AGENT_STAGES:
            body = stage(self.code, name)
            calls = helper_calls(body)
            self.assertTrue(calls, name)
            self.assertEqual(("rs", "setup"), calls[0][:2], f"{name} does not run setup first")
        resolve = stage(self.code, "Resolve")
        self.assertLess(resolve.index("release-steps.sh setup"), resolve.index("cp "))

    def test_the_flag_file_claim_flag_decides_whether_cleanup_runs(self):
        post = post_block(self.code)
        self.assertRegex(post, r"def claimed\s*=\s*sh\(returnStatus:\s*true,\s*script:\s*'test -f \"\$RELEASE_WORK_DIR/claim\.flag\"'\)\s*==\s*0")
        cleanup_at = post.index("cleanup")
        self.assertLess(post.index("if (claimed)"), cleanup_at)
        self.assertEqual(1, post.count("cleanup"))
        self.assertNotIn("cleanup", self.code.replace(post, ""))

    def windows_when(self, name: str) -> str:
        when = block_after(stage(self.code, name), r"\bwhen\s*\{")
        self.assertIsNotNone(when, f"{name} has no when")
        return when

    def test_every_windows_stage_runs_whenever_the_mode_is_release_with_no_optional_condition(self):
        """D-059: signing is mandatory, so no HAS_CERT or WINDOWS_SIGNING condition is left on the three stages, and the
        Windows stages are on windows-sign with beforeAgent."""
        self.assertNotIn("HAS_CERT", self.code)
        self.assertNotIn("WINDOWS_SIGNING", self.text)
        for name in WINDOWS_STAGES:
            when = self.windows_when(name)
            self.assertIn("beforeAgent true", when, name)
            self.assertIn("params.RUN_MODE == 'release'", when, name)
            self.assertNotRegex(when, r"HAS_CERT|WINDOWS_SIGNING|allOf", name)

    def test_a_windows_preflight_stage_on_the_windows_sign_agent_runs_before_the_claim_and_resolve_stops_without_the_certificate_file(self):
        """Task T056, US3-AS1, D-058, D-059: before the tag is claimed, a preflight on windows-sign checks that the
        certificate with the pinned SHA-256 is present, and otherwise stops and says to log in to SimplySign; in release
        mode Resolve fails when jenkins/release-certificate.json is missing."""
        resolve = stage(self.code, "Resolve")
        self.assertIn("fileExists('jenkins/release-certificate.json')", resolve)
        self.assertRegex(resolve, r"\berror\s*\(.*release-certificate\.json")
        names = [n for n, _, _ in self.stages]
        self.assertEqual(names.index("Resolve") + 1, names.index("Windows preflight"))
        self.assertLess(names.index("Windows preflight"), names.index("Claim"))
        body = stage(self.code, "Windows preflight")
        self.assertRegex(body, r"timeout\(time:\s*\d+,\s*unit:\s*'MINUTES'\)")
        self.assertIn("RELEASE_CERT_SHA256", body)
        self.assertIn("SimplySign", body)
        self.assertRegex(body, r"\bexit\s+1\b|\berror\s*\(")

    def test_the_windows_sign_stage_runs_after_build_on_windows_sign_with_the_certificate_sha256_constant_and_returns_a_report_and_the_signed_file_by_stash(self):
        """Task T056, US3-AS1, US3-AS3, D-058, D-059: the built exe is stashed by Build, unstashed on windows-sign and
        signed with signtool, the pinned certificate and an RFC 3161 timestamp; windows-report.txt (Status,
        SignerSha256, Timestamped, FileSha256) and the signed exe come back by one stash. The pin is one constant."""
        value = constant(self.code, "RELEASE_CERT_SHA256")
        self.assertIsNotNone(value)
        self.assertRegex(value, HEX64)
        self.assertEqual(1, len(re.findall(r"(?m)^[ \t]*RELEASE_CERT_SHA256\s*=", self.code)))
        names = [n for n, _, _ in self.stages]
        self.assertIn("Resign sums", names)
        self.assertEqual(names.index("Build") + 1, names.index("Windows sign"))
        self.assertEqual(names.index("Windows sign") + 1, names.index("Resign sums"))
        build, body = stage(self.code, "Build"), stage(self.code, "Windows sign")
        self.assertRegex(build, r"\bstash\b[^\n]*venom\.windows-amd64\.exe")
        self.assertRegex(body, r"timeout\(time:\s*\d+,\s*unit:\s*'MINUTES'\)")
        self.assertRegex(body, r"\bunstash\b")
        for word in ("signtool", "RELEASE_CERT_SHA256", "/tr", "/td", "Status=", "SignerSha256=", "Timestamped=", "FileSha256="):
            self.assertIn(word, body)
        returned = [line for line in body.splitlines() if re.search(r"\bstash\b", line) and "unstash" not in line]
        self.assertTrue(returned, "no stash returns the report and the signed file")
        self.assertIn("windows-report.txt", returned[0])
        self.assertIn("venom.windows-amd64.exe", returned[0])

    def test_the_resign_sums_stage_places_the_signed_exe_verifies_it_rebuilds_the_sums_and_stashes_them_unsigned(self):
        """D-059: on docker-e2e, after Windows sign, the signed file and report are unstashed, the exe is placed in
        dist, windows-verify reads the report, resign-sums rebuilds SHA256SUMS, and SHA256SUMS goes out as
        sums-unsigned. windows-mode and minisign are not part of it."""
        body = stage(self.code, "Resign sums")
        self.assertRegex(body, r"agent\s*\{\s*label\s+'docker-e2e'\s*\}")
        self.assertRegex(body, r"unstash\s+'windows-signed'")
        commands = [c for h, c, _ in helper_calls(body) if h == "rs"]
        self.assertEqual(["setup", "windows-verify", "resign-sums"], commands)
        self.assertRegex(body, r"cp\s+\"\$WORKSPACE/venom\.windows-amd64\.exe\"\s+\"\$RELEASE_WORK_DIR/dist/\"")
        self.assertIn("windows-report.txt", re.search(r"rs windows-verify[^\n]*", body).group(0))
        self.assertLess(body.index("cp \"$WORKSPACE/venom.windows-amd64.exe\""), body.index("rs windows-verify"))
        self.assertLess(body.index("rs windows-verify"), body.index("rs resign-sums"))
        self.assertLess(body.index("rs resign-sums"), body.index("stash name: 'sums-unsigned'"))
        stashed = [line for line in body.splitlines() if "stash name: 'sums-unsigned'" in line]
        self.assertIn("SHA256SUMS", stashed[0])
        self.assertNotIn("withCredentials", body)
        for n, other, _ in self.stages:
            if n != "Resign sums":
                self.assertEqual([], [c for h, c, _ in helper_calls(other) if c in ("windows-verify", "resign-sums")], n)

    def test_the_windows_sign_sums_stage_signs_sha256sums_as_a_detached_cms_with_the_pinned_certificate(self):
        """D-059: inline PowerShell on windows-sign: unstash sums-unsigned, find the certificate whose SHA-256 is
        RELEASE_CERT_SHA256 in the store, SignedCms detached over SHA256SUMS, SHA-256 digest, WholeChain, DER written
        as SHA256SUMS.p7s, returned by the sums-signed stash."""
        names = [n for n, _, _ in self.stages]
        self.assertIn("Resign sums", names)
        self.assertIn("Windows sign sums", names)
        self.assertEqual(names.index("Resign sums") + 1, names.index("Windows sign sums"))
        body = stage(self.code, "Windows sign sums")
        self.assertRegex(body, r"unstash\s+'sums-unsigned'")
        for word in ("SignedCms", "ContentInfo", "ComputeSignature", "CmsSigner", "WholeChain", "Cert:", "RELEASE_CERT_SHA256",
                     "SHA256SUMS", "SHA256SUMS.p7s", "WriteAllBytes", "Encode()"):
            self.assertIn(word, body, word)
        self.assertRegex(body, r"2\.16\.840\.1\.101\.3\.4\.2\.1|Oid\W+SHA256", "no SHA-256 digest algorithm")
        self.assertRegex(body, r"SignedCms\([^)]*\$true", "the signature is not detached")
        returned = [line for line in body.splitlines() if "stash name: 'sums-signed'" in line]
        self.assertTrue(returned, "no sums-signed stash")
        self.assertIn("SHA256SUMS.p7s", returned[0])
        self.assertNotIn("signtool", body)

    def test_the_seal_stage_places_the_cms_signature_verifies_it_and_requests_the_timestamp(self):
        """D-059: on docker-e2e, unstash sums-signed, place SHA256SUMS.p7s in dist, rs sign-verify (openssl cms and
        the signer's SHA-256 against the pin), rs timestamp (RELEASE_TSA_URL), then Checks."""
        names = [n for n, _, _ in self.stages]
        self.assertIn("Windows sign sums", names)
        self.assertIn("Seal", names)
        self.assertEqual(names.index("Windows sign sums") + 1, names.index("Seal"))
        self.assertEqual(names.index("Seal") + 1, names.index("Checks"))
        body = stage(self.code, "Seal")
        self.assertRegex(body, r"agent\s*\{\s*label\s+'docker-e2e'\s*\}")
        self.assertRegex(body, r"unstash\s+'sums-signed'")
        commands = [c for h, c, _ in helper_calls(body) if h == "rs"]
        self.assertEqual(["setup", "sign-verify", "timestamp"], commands)
        copy = re.search(r"cp\s+\"\$WORKSPACE/SHA256SUMS\.p7s\"\s+\"\$RELEASE_WORK_DIR/dist/\"", body)
        self.assertTrue(copy, "SHA256SUMS.p7s is not placed in dist")
        self.assertLess(body.index("unstash"), copy.start())
        self.assertLess(copy.start(), body.index("rs sign-verify"))
        self.assertLess(body.index("rs sign-verify"), body.index("rs timestamp"))
        self.assertNotIn("withCredentials", body)
        self.assertIn("RELEASE_TSA_URL", self.code)

    def test_the_windows_agent_runs_inline_steps_only_and_no_repository_script_or_checkout(self):
        """Task T056, D-058, D-059: nothing from the repository runs on the maker's Windows machine: no checkout, no
        shell helper, no copy of the scripts, no path under jenkins/, no git; the three stages are inline PowerShell."""
        for name in WINDOWS_STAGES:
            body = stage(self.code, name)
            self.assertNotRegex(body, r"\bcheckout\b", name)
            self.assertEqual([], helper_calls(body), name)
            for word in ("release-steps", "release-github", "release-image", "release-leak", "RELEASE_BIN", "jenkins/",
                         "shellPrelude", "git "):
                self.assertNotIn(word, body, f"{name} uses {word}")
            self.assertRegex(body, r"\b(powershell|pwsh|bat)\s*\(", name)
            self.assertNotRegex(body, r"\bsh\s*\(", name)

    def test_the_job_parameter_windows_signing_and_the_unsigned_fallback_are_gone(self):
        """D-059: signing is mandatory, so the Jenkinsfile no longer reads WINDOWS_SIGNING or calls windows-mode unsigned."""
        self.assertNotIn("WINDOWS_SIGNING", self.text)
        self.assertNotIn("WINDOWS_SIGNING", read_repo(RELEASE_JOB))
        self.assertNotIn("rs windows-mode unsigned", self.code)

    def test_no_credential_is_bound_on_the_windows_agent(self):
        """Task T056, NFR-002, D-058, D-059: the one-time code is typed into SimplySign Desktop on the maker's phone and
        nothing is stored in Jenkins: no withCredentials, no credential id in the Windows stages."""
        for name in WINDOWS_STAGES:
            body = stage(self.code, name)
            self.assertNotIn("withCredentials", body, name)
            self.assertNotRegex(body, r"(?i)credential|token|password", name)
            for cid in (TOKEN_ID, REGISTRY_ID):
                self.assertNotIn(cid, body)

    def test_concurrent_builds_are_disabled_and_the_time_limit_is_set(self):
        self.assertRegex(self.code, r"options\s*\{[^}]*disableConcurrentBuilds\(\)")
        self.assertEqual("60", constant(self.code, "RELEASE_TIME_LIMIT_MINUTES"))
        for name in RELEASE_ONLY_AGENT_STAGES + WINDOWS_STAGES:
            self.assertRegex(stage(self.code, name), r"timeout\(time:\s*\d+,\s*unit:\s*'MINUTES'\)", name)
        self.assertRegex(self.code, r"deadline\s+\"\$RELEASE_START\"\s+3600\s+\"\$RELEASE_WAIT_TOTAL\"")
        self.assertRegex(self.code, r"\blimit\b|timeout \"\$\(bash")
        self.assertEqual(1, len(re.findall(r"(?m)^def shellPrelude\(\)", self.code)))
        self.assertEqual(1, len(re.findall(r"(?m)^rs\(\) \{", self.code)))

    def test_the_image_stage_follows_the_publish_stage(self):
        names = [n for n, _, _ in self.stages]
        self.assertEqual(names.index("Draft and publish") + 1, names.index("Image"))
        publish = stage(self.code, "Draft and publish")
        commands = [c for h, c, _ in helper_calls(publish)]
        self.assertLess(commands.index("publish"), commands.index("verify-public"))
        self.assertEqual(names[-1], "Image")

    def test_the_record_stage_is_in_post_always(self):
        post = post_block(self.code)
        always = block_after(post, r"\balways\s*\{")
        self.assertIsNotNone(always)
        self.assertIn('release-steps.sh" record', always)
        self.assertIn("archiveArtifacts", always)
        self.assertIn("RELEASE_STATUS", always)
        for word in ("success", "failed", "aborted"):
            self.assertIn(f"'{word}'", always)
        self.assertNotRegex(post, r"\b(success|failure|unstable|cleanup)\s*\{")
        for n, body, _ in self.stages:
            self.assertNotIn("release-steps.sh\" record", body)


class ReleaseJob(unittest.TestCase):
    """T038: jenkins/jobs/venom-release.xml, the pattern of PublishJob."""

    def test_the_job_xml_is_well_formed_and_names_the_script(self):
        text = read_repo(RELEASE_JOB)
        root = ET.fromstring(text)
        self.assertEqual("flow-definition", root.tag)
        definition = root.find("definition")
        self.assertEqual("org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition", definition.get("class"))
        self.assertEqual("jenkins/release.Jenkinsfile", definition.findtext("scriptPath"))
        self.assertEqual("true", definition.findtext("lightweight"))
        branches = [b.findtext("name") for b in definition.iter("hudson.plugins.git.BranchSpec")]
        self.assertEqual(["*/main"], branches)
        params = root.find("properties/hudson.model.ParametersDefinitionProperty/parameterDefinitions")
        names = [p.findtext("name") for p in params]
        self.assertEqual(["RUN_MODE", "RELEASE_TAG", "RELEASE_NOTES", "RELEASE_BASE"], names)
        self.assertNotIn("minisign", text.lower(), "D-059: the job names no minisign")
        self.assertNotIn("BRANCH", names)
        kinds = {p.findtext("name"): p.tag for p in params}
        self.assertEqual("hudson.model.ChoiceParameterDefinition", kinds["RUN_MODE"])
        self.assertEqual("hudson.model.TextParameterDefinition", kinds["RELEASE_NOTES"])
        choices = [s.text for s in params[0].iter("string")]
        self.assertEqual(["release", "image-only"], choices)
        for p in params:
            if p.tag != "hudson.model.ChoiceParameterDefinition":
                self.assertEqual("", p.findtext("defaultValue") or "", p.findtext("name"))
        discarder = root.find("properties/jenkins.model.BuildDiscarderProperty/strategy")
        self.assertEqual("hudson.tasks.LogRotator", discarder.get("class"))
        self.assertEqual("30", discarder.findtext("numToKeep"))
        self.assertEqual("false", root.findtext("disabled"))
        self.assertIsNone(root.find("triggers/*"), "a release starts by hand, never from a trigger")
        self.assertNotIn("authToken", text)
        publish = ET.fromstring(read_repo(PUBLISH_JOB))
        for path in ("definition/scm/userRemoteConfigs/hudson.plugins.git.UserRemoteConfig/url",
                     "definition/scm/userRemoteConfigs/hudson.plugins.git.UserRemoteConfig/credentialsId"):
            self.assertEqual(publish.findtext(path), root.findtext(path), path)


if __name__ == "__main__":
    unittest.main()
