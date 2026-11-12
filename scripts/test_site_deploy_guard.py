#!/usr/bin/env python3
"""Guard of deploy/ against cluster values and the compliance records of story #3623, task #3679.
Offline, standard library only. Run: python scripts/test_site_deploy_guard.py
"""
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_site import ROOT  # noqa: E402

DEPLOY = ROOT / "deploy"
EXPECTED_FILES = {"site/Dockerfile", "site/nginx.conf", "site/README.md"}
PRIVATE_NAMES = "docs/compliance/site-private-names.txt"
ISMS = "docs/compliance/isms-change-3623.md"
README = "deploy/site/README.md"
PIPELINES = "docs/compliance/pipelines-venom.md"
THREAT_MODEL = "docs/architecture/threat-model.md"
PUBLIC_HOST = "venom.pluries.dev"
HOST_ALLOWLIST = {"localhost", "example.test"}
TEMPLATE_ROWS = ("Objective", "Changes", "Environments", "Technologies", "Testing", "Controls",
                 "Risk accepted", "Fallback", "Approvals")

IPV4 = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")
HOST = re.compile(r"\b[a-z0-9-]+(\.[a-z0-9-]+)*\.(com|dev|io|net|org|local|internal|lan|cloud|app|test|example)\b", re.I)
HOST_PORT = re.compile(r"\b([a-z0-9][a-z0-9.-]*):\d{2,5}/", re.I)
PLURIES = re.compile(r"pluries\.dev", re.I)
VOCABULARY = re.compile(
    r"\bnamespace\b|\bingress\b|kind:\s*(Deployment|Service|Ingress|Kustomization)\b|argocd|cert-manager|kustomization",
    re.I)
DIGEST_LINE = re.compile(r"^\s*digest:", re.M)


def read_text(rel: str) -> str:
    path = ROOT / rel
    if not path.is_file():
        raise AssertionError(f"{rel} does not exist")
    return path.read_text(encoding="utf-8")


def private_names() -> list[str]:
    names = []
    for line in read_text(PRIVATE_NAMES).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(line.lower())
    return names


def registry_hosts(text: str) -> list[str]:
    found = [m.group(0) for m in HOST.finditer(text)]
    found += [m.group(1) for m in HOST_PORT.finditer(text)]
    return [h for h in found if h.lower() not in HOST_ALLOWLIST]


def deploy_problems(deploy_dir: Path) -> list[str]:
    problems = []
    files = {p.relative_to(deploy_dir).as_posix(): p for p in deploy_dir.rglob("*") if p.is_file()}
    for rel in sorted(set(files) - EXPECTED_FILES):
        problems.append(f"unexpected file {rel}")
    for rel in sorted(EXPECTED_FILES - set(files)):
        problems.append(f"missing file {rel}")
    names = private_names()
    for rel, path in sorted(files.items()):
        text = path.read_text(encoding="utf-8", errors="replace")
        if PLURIES.search(text):
            problems.append(f"{rel}: a pluries.dev name")
        if IPV4.search(text):
            problems.append(f"{rel}: an IPv4 address")
        for host in registry_hosts(text):
            problems.append(f"{rel}: a host name {host}")
        if VOCABULARY.search(text):
            problems.append(f"{rel}: Kubernetes or GitOps vocabulary")
        if DIGEST_LINE.search(text):
            problems.append(f"{rel}: a digest: line")
        lowered = text.lower()
        for name in names:
            if name in lowered:
                problems.append(f"{rel}: the private name {name}")
    return problems


def table_rows(text: str) -> dict[str, str]:
    rows = {}
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|", 1)]
        if line.startswith("|") and len(cells) == 2:
            rows[cells[0]] = cells[1]
    return rows


def section(text: str, heading: re.Pattern) -> str:
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#") and heading.search(line):
            body = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("#"):
                    break
                body.append(nxt)
            return "\n".join(body)
    return ""


class DeployGuard(unittest.TestCase):
    """AC-4, NFR-05: deploy/ holds the image files only and no value of the cluster."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.copy = self.tmp / "deploy"
        shutil.copytree(DEPLOY, self.copy)

    def planted(self, rel: str, content: str) -> list[str]:
        path = self.copy / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        before = path.read_text(encoding="utf-8") if path.exists() else None
        path.write_text((before or "") + content, encoding="utf-8")
        self.assertNotEqual(before, path.read_text(encoding="utf-8"))
        return deploy_problems(self.copy)

    def test_real_tree_holds_exactly_the_three_image_files(self):
        files = {p.relative_to(DEPLOY).as_posix() for p in DEPLOY.rglob("*") if p.is_file()}
        self.assertEqual(files, EXPECTED_FILES)

    def test_real_tree_has_no_cluster_value(self):
        self.assertEqual(deploy_problems(DEPLOY), [])

    def test_a_planted_file_is_reported(self):
        self.assertTrue(any("unexpected file site/extra.yaml" in p for p in self.planted("site/extra.yaml", "a: b\n")))

    def test_an_ingress_file_is_reported(self):
        problems = self.planted("site/ingress.yaml", "kind: Ingress\n")
        self.assertTrue(any("unexpected file site/ingress.yaml" in p for p in problems))
        self.assertTrue(any("vocabulary" in p for p in problems))

    def test_a_registry_host_is_reported(self):
        problems = self.planted("site/Dockerfile", "FROM registry.internal/venom/site\n")
        self.assertTrue(any("a host name registry.internal" in p for p in problems))

    def test_a_registry_host_with_a_port_is_reported(self):
        problems = self.planted("site/README.md", "\ndocker push myregistry:5000/venom-site\n")
        self.assertTrue(any("myregistry" in p for p in problems))

    def test_the_public_site_name_is_reported(self):
        problems = self.planted("site/README.md", f"\nServed at https://{PUBLIC_HOST}/\n")
        self.assertTrue(any("pluries.dev" in p for p in problems))

    def test_an_ip_address_is_reported(self):
        problems = self.planted("site/nginx.conf", "# upstream 10.20.30.40\n")
        self.assertTrue(any("IPv4" in p for p in problems))

    def test_a_namespace_line_is_reported(self):
        problems = self.planted("site/README.md", "\nnamespace: venom-site\n")
        self.assertTrue(any("vocabulary" in p for p in problems))

    def test_gitops_tool_names_are_reported(self):
        for word in ("argocd", "cert-manager", "kustomization"):
            with self.subTest(word=word):
                shutil.rmtree(self.copy)
                shutil.copytree(DEPLOY, self.copy)
                self.assertTrue(any("vocabulary" in p for p in self.planted("site/README.md", f"\nsee {word}\n")))

    def test_a_digest_line_is_reported(self):
        problems = self.planted("site/README.md", "\n  digest: sha256:abc\n")
        self.assertTrue(any("digest:" in p for p in problems))

    def test_a_private_name_is_reported(self):
        name = private_names()[0]
        problems = self.planted("site/README.md", f"\nowned by {name}\n")
        self.assertTrue(any("private name" in p for p in problems))

    def test_the_allowlisted_hosts_are_not_reported(self):
        self.assertEqual(self.planted("site/README.md", "\nhttp://localhost:8080/ and https://example.test/\n"), [])


class Records(unittest.TestCase):
    """AC-1, AC-5, NFR-12: the records say the publish job is one call of the library's imMirrorPublishPipeline."""

    OLD_DESIGN = ("Guard stage", "plain pipeline", "SCM poll", "refuses any checked-out ref")

    # Scenario US2-AS7: the ISMS change request record (pinned existing test).
    def test_isms_change_has_the_classification_header(self):
        rows = table_rows(read_text(ISMS))
        self.assertEqual(rows.get("Classification"), "Restricted")
        self.assertEqual(rows.get("Status"), "Draft")
        for key in ("Owner", "Controlled copy", "Last updated"):
            self.assertTrue(rows.get(key), key)
        self.assertIn("#3623", rows.get("Work items", ""))
        self.assertIn("#3680", rows.get("Work items", ""))

    # Scenario US2-AS7: the ISMS change request record (pinned existing test).
    def test_isms_change_fills_the_nine_template_rows(self):
        rows = table_rows(read_text(ISMS))
        for key in TEMPLATE_ROWS:
            with self.subTest(row=key):
                self.assertTrue(rows.get(key, "").strip(), f"row {key} missing or empty")

    # Scenario US2-AS7: the ISMS change request record (pinned existing test).
    def test_isms_change_names_the_library_call_the_credentials_and_the_agents(self):
        text = read_text(ISMS)
        needles = ("imMirrorPublishPipeline", "sdlc@v0.7.0", "github-mirror-key", "site-registry",
                   "site-gitops-key", "SITE_IMAGE_REPOSITORY", "GITOPS_URL", "GITOPS_OVERLAY",
                   "GITOPS_BRANCH", "GITOPS_KNOWN_HOSTS", "venom-publish", PUBLIC_HOST, "GitOps",
                   "docker-e2e", "Go agent", "approval", "previous digest")
        for needle in needles:
            with self.subTest(needle=needle):
                self.assertIn(needle, text)
        self.assertTrue("venom/venom-branch/main" in text or "upstream trigger" in text,
                        "the upstream trigger is not named")

    # Scenario US2-AS7: the ISMS change request record (pinned existing test).
    def test_isms_change_does_not_describe_the_old_design(self):
        text = read_text(ISMS)
        for phrase in self.OLD_DESIGN:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, text)

    # Scenario US2-AS7: the ISMS change request record (pinned existing test).
    def test_isms_change_holds_no_ip_and_no_registry_host(self):
        text = read_text(ISMS).replace(PUBLIC_HOST, "")
        self.assertIsNone(IPV4.search(text))
        self.assertEqual(registry_hosts(text), [])

    def test_image_readme_no_longer_says_it_is_not_deployed(self):
        text = read_text(README)
        self.assertNotRegex(text, r"(?i)not pushed")
        self.assertNotRegex(text, r"(?i)not deployed")

    def test_image_readme_publish_section_names_the_scripts_and_the_library(self):
        body = section(read_text(README), re.compile(r"Publish"))
        self.assertTrue(body, "no Publish section")
        for needle in ("jenkins/publish-steps.sh", "jenkins/image-steps.sh", "jenkins/gitops-steps.sh",
                       "jenkins/publish.Jenkinsfile", "imMirrorPublishPipeline", "private GitOps repository"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)
        self.assertRegex(body, r"(?i)\bdigest\b")
        for phrase in ("Guard stage", "plain pipeline"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, body)

    def test_image_readme_holds_no_cluster_value(self):
        self.assertEqual(deploy_problems(DEPLOY), [])

    def test_pipelines_change_log_has_the_row_of_the_library_route(self):
        rows = [l for l in read_text(PIPELINES).splitlines() if l.startswith("| 2026-11-05 |")]
        self.assertTrue(any(all(n in l for n in ("#3680", "venom-publish", "imMirrorPublishPipeline", "sdlc@v0.7.0"))
                            for l in rows), rows)

    def test_pipelines_change_log_has_no_trace_of_the_old_design(self):
        rows = [l for l in read_text(PIPELINES).splitlines() if l.startswith("| 2026-11-05 |")]
        for l in rows:
            if "#3623" in l and "venom-publish" in l:
                for phrase in ("Guard stage", "plain pipeline job"):
                    with self.subTest(phrase=phrase):
                        self.assertNotIn(phrase, l)
        for l in read_text(PIPELINES).splitlines():
            if "venom-publish" in l:
                self.assertNotIn("SCM poll of `main`", l)

    def test_adr_0008_amendment_describes_the_library_route(self):
        matches = sorted((ROOT / "docs" / "adr").glob("0008-*.md"))
        self.assertEqual(len(matches), 1)
        body = section(matches[0].read_text(encoding="utf-8"), re.compile(r"Amendment"))
        self.assertTrue(body, "no heading containing Amendment")
        for needle in ("2026-11-05", "venom-publish", "#3680", "Job Restrictions", "ancestor", "docker-e2e"):
            with self.subTest(needle=needle):
                self.assertIn(needle, body)
        for phrase in ("its first stage refuses any checked-out ref", "Guard stage"):
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, body)

    # Scenario US2-AS7: the ISMS change request record (pinned existing test).
    def test_isms_change_says_a_public_correction_is_a_new_commit_never_a_force(self):
        """Task T009, NFR-003: one line says a public correction is a new commit and never a force push."""
        lines = [l.lower() for l in read_text(ISMS).splitlines()]
        self.assertTrue(any("correct" in l and "new commit" in l and "force" in l for l in lines),
                        "no line holds correction, new commit and force together")

    # Scenario US2-AS7: the ISMS change request record (pinned existing test).
    def test_isms_change_records_the_deploy_key_rotation_and_the_branch_rules(self):
        """Task T009, NFR-005: the deploy key rotation, and the branch rules that forbid force push and deletion."""
        lines = [l.lower() for l in read_text(ISMS).splitlines()]
        self.assertTrue(any("deploy key" in l and "rotat" in l for l in lines), "no line on the deploy key rotation")
        self.assertTrue(any(("branch rule" in l or "ruleset" in l) and "force" in l and "deletion" in l
                            for l in lines), "no line on branch rules that forbid force push and deletion")

    def threat_row(self, row_id: str) -> list[str]:
        for line in read_text(THREAT_MODEL).splitlines():
            if line.startswith(f"| {row_id} |"):
                return [c.strip() for c in line.strip().strip("|").split("|")]
        return []

    def test_threat_model_has_rows_t45_and_t46(self):
        """Task T020, US2-AS6: T-45 spoofing of the fork, T-46 release files, checksums and the signing key."""
        needles = {"T-45": ("spoof", "fork"), "T-46": ("SHA256SUMS", "signing key", "minisign.pub")}
        for row_id, words in needles.items():
            with self.subTest(row=row_id):
                cells = self.threat_row(row_id)
                self.assertGreaterEqual(len(cells), 7, f"row {row_id} is missing")
                self.assertRegex(cells[2], r"^[STRIDE](, ?[STRIDE])*$", "no STRIDE letter")
                self.assertTrue(cells[4], "no mitigation")
                self.assertRegex(cells[-1], r"(?i)owner", "no owner")
                for word in words:
                    self.assertIn(word.lower(), " ".join(cells).lower())

    def test_adr_0015_and_adr_0016_exist_with_the_madr_sections_and_the_header(self):
        """Task T021, US2-AS8: ADR-0016 also holds the heading Publish channel and approvals."""
        for number in ("0015", "0016"):
            with self.subTest(adr=number):
                matches = sorted((ROOT / "docs" / "adr").glob(f"{number}-*.md"))
                self.assertEqual(len(matches), 1, f"docs/adr/{number}-*.md")
                text = matches[0].read_text(encoding="utf-8")
                self.assertRegex(text, rf"(?m)\A# ADR-{number}: \S")
                self.assertRegex(text, r"(?m)^Status: (Proposed|Accepted)\s+Date: \d{4}-\d\d-\d\d")
                self.assertRegex(text, r"(?m)^Work items: .*Classification: Restricted\s*$")
                for heading in ("Context", "Decision", "Alternatives", "Consequences", "Compliance"):
                    self.assertRegex(text, rf"(?m)^## {heading}\s*$", f"no section {heading}")
        adr_16 = sorted((ROOT / "docs" / "adr").glob("0016-*.md"))
        self.assertTrue(adr_16 and re.search(r"(?m)^## Publish channel and approvals\s*$",
                                             adr_16[0].read_text(encoding="utf-8")),
                        "ADR-0016 has no heading Publish channel and approvals")

    def test_spec_describes_the_library_route(self):
        text = read_text("docs/spec/public-launch.md")
        lines = text.splitlines()
        self.assertIn("GitHub first", text)
        fr10 = [l for l in lines if l.startswith("| FR-10 |")]
        self.assertEqual(len(fr10), 1)
        self.assertIn("approval", fr10[0])
        s8 = next((i for i, l in enumerate(lines) if l.startswith("**S8")), None)
        self.assertIsNotNone(s8, "no S8 section")
        end = next((i for i in range(s8 + 1, len(lines)) if lines[i].startswith("**S")), len(lines))
        s8_text = " ".join(lines[s8:end])
        self.assertIn("live proof", s8_text)
        self.assertIn("#3680", s8_text)
        s3 = next((i for i, l in enumerate(lines) if l.startswith("**S3")), None)
        self.assertIsNotNone(s3, "no S3 section")
        third = next((l for l in lines[s3 + 1:] if l.startswith("3.")), "")
        self.assertIn("approv", third)


if __name__ == "__main__":
    unittest.main()
