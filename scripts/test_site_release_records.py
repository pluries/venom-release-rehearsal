#!/usr/bin/env python3
"""Static tests of files the release feature edits or pins (feature 011). T015 only for now: the Makefile of the CLI.
ReleaseMakefile (task T015) reads cmd/venom/Makefile, a file the public repository ships, so the class runs in the
public tree too. Later tasks (T043 to T045, T059, T077) add their classes to this file.
Offline, standard library only. Run: python scripts/test_site_release_records.py
"""
import re
import unittest

from test_build_site import ROOT

MAKEFILE = ROOT / "cmd" / "venom" / "Makefile"


def assignment(text: str, name: str) -> str | None:
    match = re.search(rf"(?m)^{name}\s*[:?+]?=\s*(.*)$", text)
    return match.group(1) if match else None


@unittest.skipUnless(MAKEFILE.is_file(), "cmd/venom/Makefile is not in this tree")
class ReleaseMakefile(unittest.TestCase):
    """Task T015. Covers US1-AS4, NFR-006."""

    def test_make_build_passes_trimpath_and_no_vcs_stamp(self):
        """Task T015, US1-AS4, NFR-006: LDFLAGS still sets venom.Version from GIT_DESCRIBE (pinned, green today);
        BUILD_MODE adds -trimpath and -buildvcs=false (red until T017), the only line .build/go.mk reads for it."""
        text = MAKEFILE.read_text(encoding="utf-8")
        ldflags = assignment(text, "LDFLAGS")
        self.assertIsNotNone(ldflags, "LDFLAGS is not assigned")
        self.assertIn("-X github.com/ovh/venom.Version=", ldflags)
        self.assertIn("GIT_DESCRIBE", ldflags)
        mode = assignment(text, "BUILD_MODE")
        self.assertIsNotNone(mode, "BUILD_MODE is not assigned in cmd/venom/Makefile")
        flags = mode.split()
        self.assertIn("-trimpath", flags)
        self.assertIn("-buildvcs=false", flags)


def read_record(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def header_ok(case: unittest.TestCase, text: str) -> None:
    case.assertRegex(text, r"(?m)^\| Classification \| Restricted \|")
    for field in ("Status", "Owner", "Controlled copy", "Last updated"):
        case.assertRegex(text, rf"(?m)^\| {field} \|", f"header field {field}")


THREAT_MODEL = ROOT / "docs" / "architecture" / "threat-model.md"
ADR_14 = ROOT / "docs" / "adr" / "0014-how-releases-are-signed.md"
ADR_16 = ROOT / "docs" / "adr" / "0016-the-release-channel-and-its-approvals.md"
ISMS_3619 = ROOT / "docs" / "compliance" / "isms-change-3619.md"
PIPELINES = ROOT / "docs" / "compliance" / "pipelines-venom.md"


@unittest.skipUnless(THREAT_MODEL.is_file(), "docs/ is not in this tree")
class Records(unittest.TestCase):
    """Tasks T043 to T046. Covers FR-014, US2-AS8, plan Design 9, 11, 13."""

    def rows(self) -> dict:
        found = {}
        for line in THREAT_MODEL.read_text(encoding="utf-8").splitlines():
            match = re.match(r"\| (T-\d+) \|", line)
            if match:
                found[match.group(1)] = [c.strip() for c in line.strip().strip("|").split("|")]
        return found

    def test_threat_model_has_rows_t47_to_t52_each_with_a_stride_letter_a_mitigation_and_an_owner(self):
        """Task T043: 7 cells (ID, component, STRIDE, threat, mitigation, residual, action); the action names an owner."""
        rows = self.rows()
        letters = {"T-47": "E", "T-48": "T", "T-49": "I", "T-50": "D", "T-51": "T", "T-52": "E"}
        for rid, letter in letters.items():
            with self.subTest(row=rid):
                self.assertIn(rid, list(rows))
                cells = rows[rid]
                self.assertEqual(len(cells), 7, rid)
                self.assertEqual(cells[2], letter)
                self.assertGreater(len(cells[4]), 40, "no mitigation")
                self.assertRegex(cells[6], r"(?i)owner")
        for rid in ("T-51", "T-52"):
            self.assertRegex(rows[rid][5], r"(?i)accepted")

    def test_threat_row_t46_names_the_certum_certificate_and_the_cms_signature_and_not_minisign(self):
        """D-059: the signature-forgery row describes the Certum certificate and the detached CMS signature."""
        self.assertIn("T-46", list(self.rows()))
        row = " ".join(self.rows()["T-46"])
        self.assertIn("Certum", row)
        self.assertIn("CMS", row)
        self.assertNotIn("minisign", row.lower())

    def test_threat_model_has_row_t53_for_the_windows_signing_agent_with_a_stride_letter_a_mitigation_and_an_owner(self):
        """D-058: the windows-sign agent; STRIDE E and T, 7 cells, owner, and the mitigation map names it."""
        rows = self.rows()
        self.assertIn("T-53", list(rows))
        cells = rows["T-53"]
        self.assertEqual(len(cells), 7)
        self.assertRegex(cells[2], r"E.*T|T.*E")
        self.assertGreater(len(cells[4]), 40, "no mitigation")
        self.assertRegex(cells[6], r"(?i)owner")
        for word in ("windows-sign", "restriction", "SHA-256", "unsigned"):
            self.assertIn(word, " ".join(cells), word)
        text = THREAT_MODEL.read_text(encoding="utf-8")
        self.assertRegex(text.split("## 5. Mitigation map")[1].split("## 6.")[0], r"T-53")
        self.assertRegex(text.split("## 8. Review")[1], r"(?m)^Update 2026-11-11 .*T-53")

    def test_adr_0014_has_a_windows_signing_section_naming_the_agent_the_certificate_pin_the_report_and_the_restriction(self):
        """D-058, D-059: the unsigned fallback and the WINDOWS_SIGNING parameter are gone, so the section no longer
        has to name them."""
        text = ADR_14.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^## Windows signing\s*$")
        section = text.split("## Windows signing")[1].split("\n## ")[0].lower()
        for word in ("windows-sign", "release-certificate.json", "windows-report.txt", "signersha256", "filesha256",
                     "restriction", "d-058", "fr-007", "not yet run"):
            self.assertIn(word, section, word)
        self.assertNotRegex(text, r"[0-9a-f]{64}", "a hash value is written")
        self.assertRegex(text, r"(?m)^Work items: .*Classification: Restricted\s*$")

    def test_t45_and_t46_no_longer_say_planned_not_built(self):
        rows = self.rows()
        for rid in ("T-45", "T-46"):
            self.assertNotIn("Planned, not built", " ".join(rows[rid]), rid)
        text = THREAT_MODEL.read_text(encoding="utf-8")
        self.assertNotIn("Planned, not built", text.split("## 5. Mitigation map")[1].split("## 6.")[0])
        self.assertRegex(text.split("## 5. Mitigation map")[1], r"T-4[7-9]|T-5[0-2]")

    def test_adr_0014_exists_with_the_madr_sections_and_the_header_and_names_custody_and_rotation(self):
        """Task T044, FR-014, D-059: custody is the Certum certificate (key in Certum's cloud, one-time code on the
        maker's phone), rotation is a renewal with a new pinned SHA-256; the minisign words (fingerprint, offline) go."""
        self.assertTrue(ADR_14.is_file(), "docs/adr/0014-how-releases-are-signed.md")
        text = ADR_14.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)\A# ADR-0014: \S")
        self.assertRegex(text, r"(?m)^Status: (Proposed|Accepted)\s+Date: \d{4}-\d\d-\d\d")
        self.assertRegex(text, r"(?m)^Work items: .*Classification: Restricted\s*$")
        for heading in ("Context", "Decision", "Alternatives", "Consequences", "Compliance"):
            self.assertRegex(text, rf"(?m)^## {heading}\s*$", heading)
        for word in ("custody", "rotation", "certum", "simplysign", "renewal", "cms", "sha-256", "revoked", "withdrawal"):
            self.assertIn(word, text.lower(), word)
        protected = ROOT / ".claude" / "protected-paths.txt"
        if protected.is_file():
            self.assertNotIn("minisign", protected.read_text(encoding="utf-8").lower())
        self.assertFalse((ROOT / "minisign.pub").exists(), "minisign.pub is dropped")
        self.assertNotRegex(text, r"[0-9a-f]{64}", "a fingerprint value is written")

    def test_adr_0016_holds_the_heading_release_channel(self):
        self.assertRegex(ADR_16.read_text(encoding="utf-8"), r"(?m)^## Release channel\s*$")

    def test_isms_change_3619_exists_with_the_header_and_names_the_certificate_the_signing_agent_the_token_the_registry_and_the_withdrawal(self):
        """Task T045, D-059: the signing key and minisign give way to the Certum certificate and the windows-sign agent."""
        self.assertTrue(ISMS_3619.is_file())
        text = ISMS_3619.read_text(encoding="utf-8")
        header_ok(self, text)
        self.assertRegex(text, r"(?m)^\| Status \| Draft")
        for word in ("certificate", "signing agent", "token", "registry", "withdrawal", "ruleset"):
            self.assertIn(word, text.lower(), word)

    def test_pipeline_record_has_the_release_job_row_planned_and_names_the_departure(self):
        """Task T046."""
        text = PIPELINES.read_text(encoding="utf-8")
        row = [l for l in text.splitlines() if "venom-release" in l]
        self.assertTrue(row, "no venom-release row")
        joined = " ".join(row)
        self.assertIn("planned", joined.lower())
        self.assertIn("rule 2", joined)
        self.assertIn("release.Jenkinsfile", joined)

    def test_validation_guide_has_rows_v9_to_v21(self):
        """Task T047."""
        text = read_record("docs/feat/3619/validation.md")
        for n in range(9, 22):
            self.assertRegex(text, rf"(?m)^\| V{n} \|", f"V{n}")


JENKINS = ROOT / "jenkins"


@unittest.skipUnless((JENKINS / "release.Jenkinsfile").is_file(), "jenkins/ is not in this tree")
class ReleaseNoMinisign(unittest.TestCase):
    """D-059: minisign is dropped; docs history and decisions may still name it, the gate files and scripts may not."""

    def test_no_file_under_jenkins_mentions_minisign(self):
        offenders = []
        for path in sorted(JENKINS.rglob("*")):
            if path.is_file() and "minisign" in path.read_text(encoding="utf-8", errors="ignore").lower():
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual([], offenders)


DOCKERFILE = ROOT / "Dockerfile"
# The index (multi-platform) digest of the alpine base image, confirmed by the maker.
ALPINE_INDEX_DIGEST = "f59abe656b692d901e184be3445e084786602108599d2a51d229c0f5a48c71bf"


@unittest.skipUnless(DOCKERFILE.is_file(), "the Dockerfile is not in this tree")
class ReleaseDockerfile(unittest.TestCase):
    """Task T059 (pinned, green today) and later T077: what the release feature must keep of the root Dockerfile.
    Covers US4-AS1, US4-AS7."""

    def test_the_entrypoint_and_the_linux_amd64_copy_are_unchanged(self):
        """Task T059, US4-AS1: the image still copies dist/venom.linux-amd64 to /usr/local/bin/venom and starts it."""
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^COPY dist/venom\.linux-amd64 /usr/local/bin/venom$")
        self.assertRegex(text, r'(?m)^ENTRYPOINT \["/usr/local/bin/venom"\]$')
        self.assertRegex(text, r'(?m)^CMD \["run", "\./tests/\*\.y\*ml"\]$')

    def test_the_image_still_runs_as_root_and_says_why(self):
        """Task T059, US4-AS7: no USER instruction, and the opening comment keeps the accepted root note (S6471)."""
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?mi)^USER\s")
        first = text.splitlines()[0]
        self.assertTrue(first.startswith("#"), first)
        self.assertIn("root", first.lower())
        self.assertIn("S6471", text.split("FROM", 1)[0])

    def test_the_base_image_is_pinned_by_digest_alone(self):
        """Task T077, US4-AS7, NFR-004: the single FROM line names the base image by its index digest, with no tag."""
        text = DOCKERFILE.read_text(encoding="utf-8")
        from_lines = re.findall(r"(?m)^FROM .*$", text)
        self.assertEqual(len(from_lines), 1, from_lines)
        self.assertRegex(from_lines[0], r"^FROM [a-z0-9./_-]+@sha256:[0-9a-f]{64}$")
        self.assertTrue(from_lines[0].endswith("@sha256:" + ALPINE_INDEX_DIGEST), from_lines[0])

    def test_the_dockerfile_copies_the_licence(self):
        """Task T077, US4-AS7, NFR-008: the image carries the LICENSE file."""
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^COPY LICENSE \S+")

    def test_the_dockerfile_copies_the_notice_next_to_the_licence(self):
        """Task T079, US4-AS7, NFR-008: the image carries the upstream attribution at the path the LICENSE is in."""
        text = DOCKERFILE.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^COPY NOTICE /usr/share/licenses/venom/NOTICE$")


NOTICE = ROOT / "NOTICE"


@unittest.skipUnless(DOCKERFILE.is_file(), "the repository root is not in this tree")
class ReleaseNotice(unittest.TestCase):
    """Task T079, NFR-008: the NOTICE file at the repository root is the upstream attribution of the fork."""

    def text(self) -> str:
        self.assertTrue(NOTICE.is_file(), "NOTICE is missing at the repository root")
        return NOTICE.read_text(encoding="utf-8")

    def test_the_notice_names_the_upstream_project_and_its_licence(self):
        text = self.text()
        self.assertIn("ovh/venom", text)
        self.assertIn("Apache", text)

    def test_the_notice_holds_no_copyright_line_of_its_own(self):
        """No copyright year or holder is invented: the file points to upstream and to LICENSE, it claims nothing."""
        for line in self.text().splitlines():
            self.assertNotRegex(line, r"(?i)copyright\s*(\(c\)|©)", line)


if __name__ == "__main__":
    unittest.main()
