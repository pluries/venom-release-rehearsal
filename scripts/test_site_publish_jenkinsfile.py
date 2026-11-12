#!/usr/bin/env python3
"""Drift guards of the publish pipeline files (story #3680, task #3693): jenkins/publish.Jenkinsfile, a parameter
block for the library's imMirrorPublishPipeline, and jenkins/jobs/venom-publish.xml, its job with an upstream trigger.
Offline, standard library only. Run: python scripts/test_site_publish_jenkinsfile.py
"""
import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_jenkinsfile import mutated, prefixed, read_repo  # noqa: E402


PUBLISH = "jenkins/publish.Jenkinsfile"
PUBLISH_JOB = "jenkins/jobs/venom-publish.xml"
NIGHTLY_JOB = "jenkins/jobs/venom-nightly.xml"

LIBRARY_LINE = "@Library('sdlc@v0.7.0') _"
CALL = "imMirrorPublishPipeline("
UPSTREAM = "venom/venom-branch/main"
PARAMETERS = ["SITE_IMAGE_REPOSITORY", "GITOPS_URL", "GITOPS_OVERLAY", "GITOPS_BRANCH", "GITOPS_KNOWN_HOSTS"]

BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
LINE_COMMENT = re.compile(r"//[^\n]*")
FORBIDDEN = [
    ("pipeline block", re.compile(r"\bpipeline\s*\{")),
    ("node block", re.compile(r"\bnode\s*\{")),
    ("sh step", re.compile(r"(?<![\w.\-/])sh(?![\w.\-/])")),
    ("withCredentials", re.compile(r"\bwithCredentials\b")),
    ("script block", re.compile(r"\bscript\s*\{")),
    ("def", re.compile(r"\bdef\b")),
    ("stage", re.compile(r"\bstage\s*\(")),
    ("steps", re.compile(r"\bsteps\s*\{")),
]
IPV4 = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")
PLURIES_NAME = re.compile(r"[\w-]+\.pluries\.dev", re.I)
HOSTNAME = re.compile(r"""['"][^'"\s]*\b[a-z0-9-]+\.(com|dev|io|net|org|local|internal|lan|cloud|app)\b[^'"\s]*['"]""", re.I)
SECRET = re.compile(r"""\b(password|passwd|token|secret|apikey)\s*[:=]\s*['"][^'"]*['"]""", re.I)
CREDENTIAL = re.compile(r"\b(sshCredentialsId|credentialsId)\s*:\s*([^,\]\n]*)")
ID_SHAPE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def code_of(text: str) -> str:
    return LINE_COMMENT.sub("", BLOCK_COMMENT.sub("", text))


def code_lines(text: str) -> list[str]:
    return [line.strip() for line in code_of(text).splitlines() if line.strip()]


def inner(code: str, key: str):
    m = re.search(rf"\b{key}\s*:\s*\[", code)
    if not m:
        return None
    depth = 1
    i = m.end()
    while i < len(code) and depth:
        depth += {"[": 1, "]": -1}.get(code[i], 0)
        i += 1
    return code[m.end():i - 1]


def string_value(block, key: str):
    m = re.search(rf"\b{key}\s*:\s*'([^']*)'", block or "")
    return m.group(1) if m else None


def break_nth(code: str, key: str, n: int) -> str:
    matches = list(re.finditer(rf"\b{key}\s*:", code))
    m = matches[n - 1]
    return code[:m.start()] + key + "x:" + code[m.end():]


def library_problems(text: str) -> list[str]:
    lines = code_lines(text)
    problems = []
    if not lines or lines[0] != LIBRARY_LINE:
        problems.append(f"library: the first code line is not {LIBRARY_LINE}")
    if code_of(text).count("@Library") != 1:
        problems.append("library: not exactly one @Library line")
    calls = code_of(text).count(CALL)
    if calls != 1:
        problems.append(f"call: expected one {CALL} call, found {calls}")
    return problems


def scripted_problems(text: str) -> list[str]:
    code = code_of(text)
    return [f"scripted: {name}" for name, pattern in FORBIDDEN if pattern.search(code)]


def key_problems(text: str) -> list[str]:
    code = code_of(text)
    problems = []
    for key, expected in (("app", "venom"), ("stack", "go")):
        if string_value(code, key) != expected:
            problems.append(f"keys: {key} is not '{expected}'")
    ado = inner(code, "ado") or ""
    if string_value(ado, "project") != "venom" or string_value(ado, "repo") != "venom":
        problems.append("keys: ado is not [project: 'venom', repo: 'venom']")
    mirror = inner(code, "mirror")
    if mirror is None:
        return problems + ["keys: no mirror block"]
    image = inner(mirror, "image")
    gitops = inner(mirror, "gitops")
    if image is None:
        problems.append("keys: no mirror.image block")
        image = ""
    if gitops is None:
        problems.append("keys: no mirror.gitops block")
        gitops = ""
    own = mirror.replace(image, "").replace(gitops, "")
    if string_value(own, "script") != "jenkins/publish-steps.sh":
        problems.append("keys: mirror.script is not jenkins/publish-steps.sh")
    if not string_value(own, "sshCredentialsId"):
        problems.append("keys: mirror.sshCredentialsId is missing")
    if not string_value(own, "approvers"):
        problems.append("keys: mirror.approvers is missing")
    if string_value(image, "script") != "jenkins/image-steps.sh":
        problems.append("keys: mirror.image.script is not jenkins/image-steps.sh")
    if not string_value(image, "credentialsId"):
        problems.append("keys: mirror.image.credentialsId is missing")
    if string_value(image, "agentLabel") != "docker-e2e":
        problems.append("keys: mirror.image.agentLabel is not docker-e2e")
    if not re.search(r"\brepository\s*:", image):
        problems.append("keys: mirror.image.repository is missing")
    if string_value(gitops, "script") != "jenkins/gitops-steps.sh":
        problems.append("keys: mirror.gitops.script is not jenkins/gitops-steps.sh")
    if not string_value(gitops, "sshCredentialsId"):
        problems.append("keys: mirror.gitops.sshCredentialsId is missing")
    outside_image = code.replace(image, "") if image else code
    if "agentLabel" in outside_image:
        problems.append("keys: an agentLabel outside the image block")
    if code.count("docker-e2e") != 1:
        problems.append("keys: the docker-e2e label does not appear exactly once")
    return problems


def literal_problems(text: str) -> list[str]:
    code = code_of(text)
    problems = []
    if IPV4.search(text):
        problems.append("literal: an IPv4 address")
    if PLURIES_NAME.search(text) or "pluries" in code.lower():
        problems.append("literal: a pluries name")
    if HOSTNAME.search(code):
        problems.append("literal: a host name")
    if SECRET.search(code):
        problems.append("literal: a password, token or secret value")
    return problems


def credential_problems(text: str) -> list[str]:
    code = code_of(text)
    problems = []
    found = CREDENTIAL.findall(code)
    if len(found) != 3:
        problems.append(f"credentials: expected three credential ids, found {len(found)}")
    for key, raw in found:
        m = re.fullmatch(r"\s*'([^']*)'\s*", raw)
        if not m or not ID_SHAPE.match(m.group(1)):
            problems.append(f"credentials: {key} {raw.strip()!r} is not a plain id")
    image = inner(inner(code, "mirror") or "", "image") or ""
    if not re.search(r"\brepository\s*:\s*params\.SITE_IMAGE_REPOSITORY\s*(,|$)", image.strip()):
        problems.append("params: the image repository is not params.SITE_IMAGE_REPOSITORY")
    return problems


def publish_problems(text: str) -> list[str]:
    return (library_problems(text) + scripted_problems(text) + key_problems(text)
            + literal_problems(text) + credential_problems(text))


def job_problems(text: str, reference: str) -> list[str]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return [f"xml: {e}"]
    problems = []
    definition = root.find("definition")
    if root.tag != "flow-definition" or definition is None or not (definition.get("class") or "").endswith("CpsScmFlowDefinition"):
        problems.append("kind: not a pipeline job from SCM")
    if root.findtext(".//scriptPath") != PUBLISH:
        problems.append(f"script: the script path is not {PUBLISH}")
    if root.findtext(".//hudson.plugins.git.BranchSpec/name") != "*/main":
        problems.append("branch: the job does not build */main")
    ref = ET.fromstring(reference)
    for tag in ("url", "credentialsId"):
        got = root.findtext(f".//hudson.plugins.git.UserRemoteConfig/{tag}")
        want = ref.findtext(f".//hudson.plugins.git.UserRemoteConfig/{tag}")
        if not want or got != want:
            problems.append(f"remote: the git {tag} differs from the nightly job's")
    defs = root.findall(".//hudson.model.StringParameterDefinition")
    names = [d.findtext("name") for d in defs]
    if sorted(n or "" for n in names) != sorted(PARAMETERS):
        problems.append(f"params: expected {PARAMETERS}, found {names}")
    for d in defs:
        if (d.findtext("defaultValue") or "") != "":
            problems.append(f"params: {d.findtext('name')} has a default value")
    trigger = root.find(".//jenkins.triggers.ReverseBuildTrigger")
    if trigger is None:
        problems.append("trigger: no upstream trigger")
    else:
        if trigger.findtext("upstreamProjects") != UPSTREAM:
            problems.append(f"trigger: the upstream project is not {UPSTREAM}")
        if trigger.findtext("threshold/name") != "SUCCESS":
            problems.append("trigger: the threshold is not SUCCESS")
    if root.find(".//hudson.triggers.SCMTrigger") is not None:
        problems.append("trigger: an SCM poll")
    if root.find(".//hudson.triggers.TimerTrigger") is not None:
        problems.append("trigger: a timer")
    if root.find(".//authToken") is not None:
        problems.append("auth: the job can be triggered with a token")
    if "anonymous" in text.lower():
        problems.append("anonymous: the job mentions anonymous")
    return problems


class PublishJenkinsfile(unittest.TestCase):
    """Drift guard of jenkins/publish.Jenkinsfile: AC-1 (the library route), AC-5 (the approval route is the
    library's), FR-10 (one pinned library call), FR-18 (image and gitops through the library), NFR-01 (no value in a
    file), NFR-03 (an exact library tag), NFR-04 (credentials by id)."""

    def real(self) -> str:
        text = read_repo(PUBLISH)
        self.assertEqual(publish_problems(text), [])
        return text

    def test_T1_real_file_pins_the_exact_library_tag_and_makes_one_call(self):
        text = self.real()
        self.assertEqual(code_lines(text)[0], LIBRARY_LINE)
        self.assertEqual(code_of(text).count(CALL), 1)

    def test_T1_floating_major_tag_is_reported(self):
        text = self.real()
        bad = mutated(text, text.replace("sdlc@v0.7.0", "sdlc@v0"))
        self.assertTrue(prefixed(library_problems(bad), "library"))

    def test_T1_code_before_the_library_line_is_reported(self):
        text = self.real()
        bad = mutated(text, "String x = 'a'\n" + text)
        self.assertTrue(prefixed(library_problems(bad), "library"))

    def test_T1_second_call_is_reported(self):
        text = self.real()
        bad = mutated(text, text + "\nimMirrorPublishPipeline(app: 'other')\n")
        self.assertTrue(prefixed(library_problems(bad), "call"))

    def test_T1_no_call_is_reported(self):
        text = self.real()
        bad = mutated(text, text.replace(CALL, "imOtherPipeline("))
        self.assertTrue(prefixed(library_problems(bad), "call"))

    def test_T2_real_file_holds_no_pipeline_code(self):
        self.assertEqual(scripted_problems(self.real()), [])

    def test_T2_planted_pipeline_code_is_reported(self):
        text = self.real()
        plants = [
            "pipeline { agent any }",
            "node { }",
            "sh 'make site'",
            "withCredentials([]) { }",
            "script { }",
            "def x = 1",
            "stage('Build') { }",
            "steps { }",
        ]
        for plant in plants:
            with self.subTest(plant=plant):
                bad = text + "\n" + plant + "\n"
                self.assertTrue(prefixed(scripted_problems(bad), "scripted"))

    def test_T3_real_file_has_the_required_keys_and_the_label_on_the_image_only(self):
        text = self.real()
        code = code_of(text)
        mirror = inner(code, "mirror")
        image = inner(mirror, "image")
        self.assertEqual(string_value(image, "agentLabel"), "docker-e2e")
        self.assertEqual(code.count("docker-e2e"), 1)
        self.assertEqual(string_value(inner(mirror, "gitops"), "script"), "jenkins/gitops-steps.sh")

    def test_T3_each_missing_key_is_reported(self):
        base = code_of(self.real())
        keys = [
            ("script", 1), ("sshCredentialsId", 1), ("approvers", 1),
            ("script", 2), ("credentialsId", 1), ("agentLabel", 1), ("repository", 1),
            ("script", 3), ("sshCredentialsId", 2),
            ("app", 1), ("stack", 1), ("ado", 1),
        ]
        for key, n in keys:
            with self.subTest(key=key, n=n):
                bad = mutated(base, break_nth(base, key, n))
                self.assertTrue(prefixed(key_problems(bad), "keys"))

    def test_T3_agent_label_outside_the_image_is_reported(self):
        base = code_of(self.real())
        bad = mutated(base, base.replace("stack:", "agentLabel: 'docker-e2e',\n  stack:", 1))
        self.assertTrue(prefixed(key_problems(bad), "keys"))

    def test_T3_other_image_label_is_reported(self):
        base = code_of(self.real())
        bad = mutated(base, base.replace("docker-e2e", "docker-other"))
        self.assertTrue(prefixed(key_problems(bad), "keys"))

    def test_T4_real_file_has_no_literal_and_takes_ids_and_the_repository_from_parameters(self):
        text = self.real()
        self.assertEqual(literal_problems(text), [])
        self.assertEqual(credential_problems(text), [])
        self.assertIn("params.SITE_IMAGE_REPOSITORY", text)

    def test_T4_planted_literals_are_reported(self):
        text = self.real()
        plants = [
            "// registry 10.20.30.40",
            "String host = 'registry.example.com'",
            "// the site at site.pluries.dev",
            "String password = 'hunter2'",
            "String token = 'abc123'",
        ]
        for plant in plants:
            with self.subTest(plant=plant):
                bad = text + "\n" + plant + "\n"
                self.assertTrue(prefixed(publish_problems(bad), "literal"))

    def test_T4_credential_that_is_not_an_id_is_reported(self):
        base = code_of(self.real())
        m = CREDENTIAL.search(base)
        bad = mutated(base, base.replace(m.group(0), f"{m.group(1)}: 'Sup3r/Secret value'", 1))
        self.assertTrue(prefixed(credential_problems(bad), "credentials"))

    def test_T4_literal_registry_path_is_reported(self):
        base = code_of(self.real())
        bad = mutated(base, base.replace("params.SITE_IMAGE_REPOSITORY", "'myorg/venom-site'"))
        self.assertTrue(prefixed(credential_problems(bad), "params"))


class PublishJob(unittest.TestCase):
    """Drift guard of jenkins/jobs/venom-publish.xml: AC-1 (the publish job), AC-5 (it starts after the branch job's
    main build), FR-10 (the script path), FR-18 (the five parameters), NFR-01 (no default value), NFR-03 (the same
    remote as the nightly job), NFR-04 (no anonymous or token trigger)."""

    def real(self) -> str:
        text = read_repo(PUBLISH_JOB)
        self.assertEqual(job_problems(text, read_repo(NIGHTLY_JOB)), [])
        return text

    def problems(self, text: str) -> list[str]:
        return job_problems(text, read_repo(NIGHTLY_JOB))

    def test_T5_real_job_is_well_formed_with_the_script_the_trigger_and_the_parameters(self):
        root = ET.fromstring(self.real())
        self.assertEqual(root.findtext(".//scriptPath"), PUBLISH)
        self.assertEqual(root.findtext(".//jenkins.triggers.ReverseBuildTrigger/upstreamProjects"), UPSTREAM)
        names = [d.findtext("name") for d in root.findall(".//hudson.model.StringParameterDefinition")]
        self.assertEqual(sorted(names), sorted(PARAMETERS))

    def test_T5_malformed_xml_is_reported(self):
        text = self.real()
        self.assertTrue(prefixed(self.problems(mutated(text, text + "<unclosed>")), "xml"))

    def test_T5_other_script_path_is_reported(self):
        text = self.real()
        bad = mutated(text, text.replace(PUBLISH, "jenkins/nightly.Jenkinsfile"))
        self.assertTrue(prefixed(self.problems(bad), "script"))

    def test_T5_other_job_kind_is_reported(self):
        text = self.real()
        bad = mutated(text, text.replace("CpsScmFlowDefinition", "CpsFlowDefinition"))
        self.assertTrue(prefixed(self.problems(bad), "kind"))

    def test_T5_other_branch_is_reported(self):
        text = self.real()
        bad = mutated(text, text.replace("*/main", "*/develop"))
        self.assertTrue(prefixed(self.problems(bad), "branch"))

    def test_T5_other_remote_or_credentials_is_reported(self):
        text = self.real()
        for old, new in (("ssh.dev.azure.com", "git.example.test"), ("ado-ssh", "other-key")):
            with self.subTest(old=old):
                bad = mutated(text, text.replace(old, new))
                self.assertTrue(prefixed(self.problems(bad), "remote"))

    def test_T5_missing_or_renamed_parameter_is_reported(self):
        text = self.real()
        for name in PARAMETERS:
            with self.subTest(name=name):
                bad = mutated(text, text.replace(f"<name>{name}</name>", f"<name>{name}_X</name>"))
                self.assertTrue(prefixed(self.problems(bad), "params"))

    def test_T5_parameter_with_a_default_value_is_reported(self):
        text = self.real()
        bad = mutated(text, re.sub(r"<defaultValue>[^<]*</defaultValue>|<defaultValue\s*/>", "<defaultValue>x</defaultValue>", text, count=1))
        self.assertTrue(prefixed(self.problems(bad), "params"))

    def test_T5_other_upstream_or_threshold_is_reported(self):
        text = self.real()
        for old, new in ((UPSTREAM, "venom/venom-branch/develop"), ("<name>SUCCESS</name>", "<name>FAILURE</name>")):
            with self.subTest(old=old):
                bad = mutated(text, text.replace(old, new))
                self.assertTrue(prefixed(self.problems(bad), "trigger"))

    def test_T5_poll_or_timer_trigger_is_reported(self):
        text = self.real()
        plants = [
            "<hudson.triggers.SCMTrigger><spec>H/5 * * * *</spec></hudson.triggers.SCMTrigger>",
            "<hudson.triggers.TimerTrigger><spec>H 2 * * *</spec></hudson.triggers.TimerTrigger>",
        ]
        for plant in plants:
            with self.subTest(plant=plant):
                bad = mutated(text, text.replace("</triggers>", plant + "</triggers>"))
                self.assertTrue(prefixed(self.problems(bad), "trigger"))

    def test_T5_auth_token_is_reported(self):
        text = self.real()
        bad = mutated(text, text.replace("</flow-definition>", "<authToken>x</authToken></flow-definition>"))
        self.assertTrue(prefixed(self.problems(bad), "auth"))

    def test_T5_anonymous_build_is_reported(self):
        text = self.real()
        bad = mutated(text, text.replace("</flow-definition>", "<!-- anonymous build -->\n</flow-definition>"))
        self.assertTrue(prefixed(self.problems(bad), "anonymous"))


if __name__ == "__main__":
    unittest.main()
