#!/usr/bin/env python3
"""Tests of the home page, the quickstart and the runnable example of the docs site (story #3621, task #3638).
They build the REAL repository, so a page or a file the site should have and lacks is an assertion failure naming it.
Offline, standard library only (the optional end-to-end test needs `go` and SITE_E2E=1). Run: python scripts/test_site_quickstart.py
"""
import contextlib
import html
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs  # noqa: E402
from test_build_site import ROOT  # noqa: E402

UPSTREAM_URL = "https://github.com/ovh/venom"
EXAMPLE = "site/examples/quickstart.yml"
SCRIPT = "scripts/check-site-example.sh"
GROUPS = ("Getting started", "Concepts", "Guides", "Executors", "Reference")
SIDE_GROUP = re.compile(r'<div class="side-group"><details[^>]*><summary>(.*?)</summary><ul>(.*?)</ul></details></div>', re.S)


def find_bash() -> str | None:
    """Git Bash first on Windows (the `bash` on PATH may be WSL's, which cannot run these scripts)."""
    if sys.platform == "win32":
        for candidate in ("C:/Program Files/Git/bin/bash.exe", "C:/Program Files (x86)/Git/bin/bash.exe"):
            if Path(candidate).is_file():
                return candidate
    return shutil.which("bash")


def build_real(out: Path) -> tuple[int, str, str]:
    """`build_site.py --root <repo> --out <out>`: (exit code, stdout, stderr)."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = bs.main(["--root", str(ROOT), "--out", str(out)])
    return code, stdout.getvalue(), stderr.getvalue()


def sidebar_groups(page_html: str) -> dict[str, list[str]]:
    """Group name -> the hrefs of its links in sidebar order, read from a built page."""
    return {html.unescape(name): re.findall(r'href="([^"]+)"', body) for name, body in SIDE_GROUP.findall(page_html)}


class RealSite(unittest.TestCase):
    """One build of the real repository shared by the tests of the class."""
    out: Path
    code: int
    stdout: str
    stderr: str

    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp())
        cls.code, cls.stdout, cls.stderr = build_real(cls.out)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, ignore_errors=True)

    def built(self, name: str) -> str:
        path = self.out / name
        if not path.is_file():
            self.fail(f"the real site should have {name} and the build did not write it (exit {self.code}; {self.stderr.strip()[:300]})")
        return path.read_text(encoding="utf-8")

    def test_the_build_succeeds_and_prints_no_error(self):
        self.assertEqual((self.code, self.stderr), (0, ""))


# ---------- T1: the quickstart ----------

class Quickstart(RealSite):
    def test_the_page_source_includes_the_example_with_the_directive(self):
        source = ROOT / "site" / "pages" / "quickstart.md"
        self.assertTrue(source.is_file(), "site/pages/quickstart.md does not exist")
        self.assertRegex(source.read_text(encoding="utf-8"), rf"(?m)^\{{\{{include {re.escape(EXAMPLE)} yaml\}}\}}\s*$")

    def test_the_rendered_page_shows_the_example_text_in_a_code_block(self):
        example = (ROOT / EXAMPLE)
        self.assertTrue(example.is_file(), f"{EXAMPLE} does not exist")
        expected = example.read_text(encoding="utf-8").replace("\r\n", "\n").strip("\n")
        page = self.built("quickstart.html")
        blocks = [html.unescape(body).replace("\r\n", "\n") for body in re.findall(r"<pre><code[^>]*>(.*?)</code></pre>", page, re.S)]
        self.assertTrue(any(expected in block for block in blocks), "no code block of quickstart.html holds the exact text of the example")

    def test_the_example_is_a_plausible_offline_venom_suite(self):
        path = ROOT / EXAMPLE
        self.assertTrue(path.is_file(), f"{EXAMPLE} does not exist")
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertRegex(text, r"(?m)^name:\s*\S")
        self.assertRegex(text, r"(?m)^testcases:\s*$")
        self.assertRegex(text, r"(?m)^\s*(- )?type:\s*exec\s*$")
        self.assertRegex(text, r"(?m)^\s*script:\s*\S")
        self.assertRegex(text, r"(?m)^\s*-\s*\S*(?:systemout|stdout)\S*\s+ShouldContainSubstring\s+\S")
        self.assertRegex(text, r"(?m)^\s*-\s*result\.code\s+ShouldEqual\s+0\s*$")
        self.assertNotRegex(text, r"(?m)type:\s*(http|sql|kafka|grpc|ssh|imap|mqtt|amqp|rabbitmq|redis|web|smtp|mongo|couchbase|ovhapi|mobile|mcp|odbc)\b")
        self.assertNotRegex(text, r"https?://")

    def test_the_quickstart_is_the_first_page_of_getting_started(self):
        groups = sidebar_groups(self.built("quickstart.html"))
        self.assertIn("Getting started", groups, "the sidebar has no Getting started group")
        self.assertEqual(groups["Getting started"][0], "quickstart.html")
        self.assertEqual(list(groups)[0], "Getting started")


# ---------- T2: the example check script ----------

@unittest.skipUnless(find_bash(), "no bash on PATH: the script check-site-example.sh cannot be run here")
class CheckScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.args_file = self.tmp / "args.txt"

    def stub(self, body: str) -> str:
        path = self.tmp / "venom-stub"
        path.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$STUB_ARGS_FILE\"\n{body}\n", encoding="utf-8", newline="\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return path.as_posix()

    def run_script(self, venom_bin: str | None) -> subprocess.CompletedProcess:
        self.assertTrue((ROOT / SCRIPT).is_file(), f"{SCRIPT} does not exist")
        env = {**os.environ, "STUB_ARGS_FILE": self.args_file.as_posix()}
        env.pop("VENOM_BIN", None)
        if venom_bin is not None:
            env["VENOM_BIN"] = venom_bin
        return subprocess.run([find_bash(), SCRIPT], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)

    def test_passes_when_venom_reports_final_status_pass(self):
        result = self.run_script(self.stub("echo 'final status: PASS'; exit 0"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check-site-example: PASS", result.stdout)

    def test_fails_when_venom_fails(self):
        result = self.run_script(self.stub("echo 'final status: FAIL'; exit 1"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("check-site-example: FAIL", result.stdout)

    def test_fails_when_venom_exits_zero_without_the_pass_line(self):
        result = self.run_script(self.stub("echo 'nothing to see'; exit 0"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("check-site-example: FAIL", result.stdout)

    def test_fails_when_the_pass_line_comes_with_a_failing_exit_code(self):
        result = self.run_script(self.stub("echo 'final status: PASS'; exit 3"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_exits_2_when_venom_bin_does_not_exist(self):
        result = self.run_script((self.tmp / "no-such-venom").as_posix())
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("check-site-example:", result.stdout + result.stderr)

    def test_exits_2_when_venom_bin_is_not_an_executable_file(self):
        result = self.run_script(self.tmp.as_posix())
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_passes_run_and_the_example_path_to_venom(self):
        self.run_script(self.stub("echo 'final status: PASS'"))
        self.assertTrue(self.args_file.is_file(), "the stub was never called")
        received = self.args_file.read_text(encoding="utf-8").split()
        self.assertEqual(received[:1], ["run"])
        self.assertIn(EXAMPLE, received)

    @unittest.skipUnless(shutil.which("go") and os.environ.get("SITE_E2E") == "1", "needs go on PATH and SITE_E2E=1: builds the real binary")
    def test_builds_and_runs_the_real_venom_on_the_example(self):
        result = self.run_script(None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("check-site-example: PASS", result.stdout)


# ---------- T3: the real site ----------

class HomeAndSite(RealSite):
    def test_the_home_page_has_a_hero_and_links_to_the_quickstart(self):
        page = self.built("index.html")
        self.assertIn('class="hero"', page)
        self.assertIn('href="quickstart.html"', page)

    def test_the_home_page_links_to_the_first_page_of_every_group_with_pages(self):
        page = self.built("index.html")
        groups = sidebar_groups(self.built("quickstart.html"))
        self.assertEqual(set(groups), set(GROUPS), "every group of site.json needs at least one page")
        for name, hrefs in groups.items():
            self.assertIn(f'href="{hrefs[0]}"', page, f"index.html does not link to {hrefs[0]}, the first page of {name}")

    def test_every_local_link_of_the_home_page_leads_to_a_built_page(self):
        page = self.built("index.html")
        for href in set(re.findall(r'href="([^"#:]+\.html)(?:#[^"]*)?"', page)):
            self.assertTrue((self.out / href).is_file(), f"index.html links to {href} and the build did not write it")

    def test_the_executors_group_has_one_page_per_readme_and_the_writing_page(self):
        expected = len(list((ROOT / "executors").glob("*/README.md"))) + 1
        hrefs = sidebar_groups(self.built("quickstart.html")).get("Executors", [])
        self.assertEqual(len(hrefs), expected, f"Executors group: {hrefs}")
        self.assertIn("writing-executors.html", hrefs)

    def test_the_reference_pages_exist(self):
        for name in ("assertions.html", "variables.html", "reports.html", "contributing.html"):
            self.built(name)

    def test_the_contributing_page_comes_from_contributing_md_and_not_from_the_readme(self):
        heading = next((line[2:].strip() for line in (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8").splitlines() if line.startswith("# ")), None)
        self.assertIsNotNone(heading, "CONTRIBUTING.md has no title")
        self.assertIn(html.escape(heading, quote=False), self.built("contributing.html"))
        leaks = [p.name for p in self.out.rglob("*") if p.is_file() and p.suffix in {".html", ".xml", ".txt", ".json"}
                 for word in ("gitpod", "shields.io", "godoc.org", "goreportcard") if word in p.read_text(encoding="utf-8", errors="replace").lower()]
        self.assertEqual(leaks, [], "the third-party badges of the README's Contributing section reached the output")

    def test_no_page_keeps_an_unresolved_placeholder(self):
        self.built("index.html")
        offenders = [p.name for p in self.out.glob("*.html") if re.search(r"\{\{\s*[\w.-]+\s*\}\}", p.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

    def test_the_home_page_and_the_quickstart_name_the_independent_fork_and_link_upstream(self):
        for name in ("index.html", "quickstart.html"):
            page = self.built(name)
            self.assertIn("independent fork", page.lower(), name)
            self.assertIn(f'href="{UPSTREAM_URL}"', page, name)

    def test_the_index_page_text_is_shown_under_the_hero(self):
        source = ROOT / "site" / "pages" / "index.md"
        self.assertTrue(source.is_file(), "site/pages/index.md does not exist")
        self.assertIn("independent fork", source.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
