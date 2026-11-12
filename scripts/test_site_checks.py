#!/usr/bin/env python3
"""Tests of the output checks and the make targets of the docs site (story #3621, task #3639).

Pinned rules the implementation must honour:
  * a finding is one string `"<path relative to the output folder, posix>: <what>: <offending text>"`; the tests read the file
    name at its start and the offending text verbatim inside it;
  * a private PATH is a private directory name followed by `/` and a path run (`docs/spec/x.md`) or an exact private file name,
    NOT preceded by a word character, `.`, `-` or `/` (so `https://example.test/docs/guide`, `site/docs/x` and `mydocs/x` are
    no finding; `href="docs/x"`, `<code>docs/x</code>` and ` docs/x` are). Bare words (docs, legacy, analysis, jenkins) never are;
  * resource loading is judged on request-making attributes only (an `<a href>` is navigation and is not judged), and an
    escaped example (`&lt;script src=...&gt;`) is text, not a tag.
Offline, standard library only. Run: python scripts/test_site_checks.py
"""
import contextlib
import io
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs  # noqa: E402
import site_checks  # noqa: E402
from test_build_site import ROOT, make_repo, write_files  # noqa: E402
from test_site_quickstart import build_real  # noqa: E402

HOST = "venom.example.test"
PRIVATE = bs.PRIVATE_PATHS
PRIVATE_FILES = ("CLAUDE.md", ".mcp.json", "Jenkinsfile", "sonar-project.properties", "scripts/publish-github.sh")
PRIVATE_DIRS = tuple(p for p in PRIVATE if p not in PRIVATE_FILES)
EXAMPLES = ("flutter.yml", "rn.yml", "failing.yml", "android.vars.example.yml", "ios.vars.example.yml", "build-sample-apps.sh",
            "handrun-settings.yml", "handrun-uikitcatalog.yml", "handrun-ios.yml")
PRIVATE_ADDRESS = re.compile(r"\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|169\.254\.\d+\.\d+)\b")


def scan(files: dict[str, str], host: str = HOST) -> list[str]:
    """`check_output` on a scratch output folder holding `files`."""
    with tempfile.TemporaryDirectory() as tmp:
        write_files(Path(tmp), files)
        return site_checks.check_output(Path(tmp), host, PRIVATE)


def page(body: str) -> dict[str, str]:
    return {"page.html": f"<html><body>{body}</body></html>"}


class Cases(unittest.TestCase):
    def assertFinding(self, files: dict[str, str], token: str, name: str = "page.html") -> None:
        found = scan(files)
        self.assertTrue(any(f.startswith(f"{name}: ") and token in f for f in found),
                        f"expected a finding for {name} naming {token!r}, got {found}")

    def assertClean(self, files: dict[str, str]) -> None:
        self.assertEqual(scan(files), [])


# ---------- T1: private path names ----------

class PrivatePathNames(Cases):
    def test_a_planted_private_path_is_reported_with_its_file_and_the_path(self):
        found = scan({"guide/intro.html": "<html><body><p>See docs/spec/public-launch.md for the plan.</p></body></html>"})
        self.assertEqual(len(found), 1, found)
        self.assertTrue(found[0].startswith("guide/intro.html: "), found[0])
        self.assertIn("docs/spec/public-launch.md", found[0])

    def test_each_private_directory_name_in_a_path_is_a_finding(self):
        for name in PRIVATE_DIRS:
            with self.subTest(name=name):
                self.assertFinding(page(f"<code>{name}/inner/file.txt</code>"), f"{name}/inner/file.txt")

    def test_each_private_file_name_is_a_finding(self):
        for name in PRIVATE_FILES:
            with self.subTest(name=name):
                self.assertFinding(page(f"<p>Edit {name} first.</p>"), name)

    def test_a_private_path_in_an_attribute_value_is_a_finding(self):
        self.assertFinding(page('<a href="docs/spec/x.md">spec</a>'), "docs/spec/x.md")

    def test_a_private_path_is_found_in_css_js_svg_txt_json_and_xml_files_too(self):
        for name in ("a.css", "a.js", "a.svg", "a.txt", "a.json", "a.xml"):
            with self.subTest(name=name):
                self.assertFinding({name: "/* see .claude/rules/x */\n \".claude/rules/x\""}, ".claude/rules/x", name)

    def test_3704_T4_a_private_value_in_the_search_index_or_an_llms_file_is_reported(self):
        planted = {
            "a private path": ("docs/spec/public-launch.md", "docs/spec/public-launch.md"),
            "a private host": ("https://dev.azure.com/org/project", "dev.azure.com"),
            "a private address": ("http://192.168.1.20/x", "192.168.1.20"),
        }
        for name in ("search-index.json", "llms.txt", "llms-full.txt"):
            for what, (value, token) in planted.items():
                with self.subTest(file=name, planted=what):
                    body = '[{"title": "Intro", "text": "' + value + '"}]' if name.endswith(".json") else "# Venom\n\nSee " + value + "\n"
                    self.assertFinding({name: body}, token, name)

    def test_3704_T4_a_clean_index_and_llms_files_give_no_finding(self):
        self.assertClean({"search-index.json": '[{"title": "Intro", "url": "/intro/", "text": "Run venom."}]',
                          "llms.txt": "# Venom\n\n- [Intro](https://venom.example.test/intro/)\n",
                          "llms-full.txt": "# Venom\n\nRun venom.\n"})

    def test_bare_words_in_prose_are_never_findings(self):
        self.assertClean(page("<p>The docs describe the legacy analysis. Jenkins and modernized setups are covered; read the docs.</p>"))

    def test_a_directory_name_in_a_url_path_on_another_host_is_no_finding(self):
        self.assertClean(page('<p>Read https://example.test/docs/guide and site/docs/x and mydocs/x and pre-docs/x.</p>'))

    def test_a_binary_file_is_not_scanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "font.woff2").write_bytes(b"\x00\x01docs/spec/x.md\x00")
            (Path(tmp) / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n.claude/rules/x")
            self.assertEqual(site_checks.check_output(Path(tmp), HOST, PRIVATE), [])


# ---------- T2: resource loading from another host ----------

class ResourceLoading(Cases):
    def test_requests_to_another_host_are_findings(self):
        cases = {
            "img": '<img src="https://evil.example/a.png">',
            "script": '<script src="//cdn.example/x.js"></script>',
            "link": '<link rel="stylesheet" href="https://fonts.example/c.css">',
            "iframe": '<iframe src="https://frame.example/f"></iframe>',
            "form": '<form action="https://post.example/p"></form>',
            "object": '<object data="https://obj.example/o"></object>',
            "embed": '<embed src="https://emb.example/e">',
            "base": '<base href="https://base.example/">',
            "srcset": '<img srcset="https://set.example/a.png 2x">',
            "refresh": '<meta http-equiv="refresh" content="0; url=https://redir.example/x">',
        }
        for name, tag in cases.items():
            with self.subTest(tag=name):
                host = re.search(r"//([\w.]+)", tag).group(1)
                self.assertFinding(page(tag), host)

    def test_css_url_and_import_to_another_host_are_findings(self):
        for name, css in {"url": "body{background:url(https://evil.example/x.png)}", "quoted": 'a{background:url("//evil.example/x.png")}',
                          "import": '@import "https://fonts.example/c.css";', "import-url": "@import url(https://fonts.example/c.css);"}.items():
            with self.subTest(css=name):
                self.assertFinding({"style.css": css}, "evil.example" if "evil" in css else "fonts.example", "style.css")

    def test_a_javascript_url_in_an_attribute_is_a_finding(self):
        self.assertFinding(page('<a href="javascript:alert(1)">x</a>'), "javascript:")

    def test_a_non_image_data_url_is_a_finding_and_an_image_one_is_not(self):
        self.assertFinding(page('<script src="data:text/javascript,alert(1)"></script>'), "data:text/javascript")
        self.assertFinding({"style.css": "@font-face{src:url(data:font/woff2;base64,AAAA)}"}, "data:font/woff2", "style.css")
        self.assertClean(page('<img src="data:image/png;base64,AAAA">'))
        self.assertClean({"style.css": "a{background:url(data:image/svg+xml;base64,AAAA)}"})

    def test_navigation_links_and_local_requests_are_no_finding(self):
        self.assertClean(page('<a href="https://pkg.go.dev/x">go</a><a href="https://github.com/ovh/venom">gh</a>'
                              '<img src="images/a.png"><script src="theme.js"></script><link rel="icon" href="favicon.svg">'
                              f'<img src="https://{HOST}/a.png"><link rel="stylesheet" href="//{HOST}/style.css">'))
        self.assertClean({"style.css": "a{background:url(fonts/a.woff2)} @import 'other.css';"})

    def test_an_escaped_tag_in_a_code_block_is_text_not_a_request(self):
        self.assertClean(page("<pre><code>&lt;script src=&quot;https://x.example/a.js&quot;&gt;&lt;/script&gt;</code></pre>"))


# ---------- T3: cookies, tracking, private hosts ----------

class CookiesAndTracking(Cases):
    def test_each_cookie_or_tracking_marker_is_a_finding(self):
        cases = {
            "set-cookie meta": ('<meta http-equiv="set-cookie" content="a=b">', "set-cookie"),
            "document.cookie": ("<script>document.cookie='a=b'</script>", "document.cookie"),
            "sendBeacon": ("<script>navigator.sendBeacon('/x')</script>", "sendBeacon"),
            "absolute fetch": ("<script>fetch('https://api.example/x')</script>", "fetch("),
            "xhr": ("<script>new XMLHttpRequest().open('GET','https://api.example/x')</script>", "XMLHttpRequest"),
            "analytics host": ('<script src="https://www.google-analytics.com/a.js"></script>', "google-analytics"),
            "tag manager": ("<p>x</p><script>var u='googletagmanager.com'</script>", "googletagmanager"),
            "plausible": ('<script data-domain="a" src="https://plausible.io/js/s.js"></script>', "plausible"),
            "matomo": ("<script>var m='matomo'</script>", "matomo"),
            "hotjar": ("<script>var h='hotjar'</script>", "hotjar"),
            "segment": ("<script>analytics.load('x'); var s='segment'</script>", "segment"),
            "fbq": ("<script>fbq('init','1')</script>", "fbq("),
            "gtag": ("<script>gtag('config','G-1')</script>", "gtag("),
            "onclick": ('<button onclick="go()">x</button>', "onclick"),
            "onerror": ('<img src="a.png" onerror="x()">', "onerror"),
        }
        for name, (body, token) in cases.items():
            with self.subTest(marker=name):
                self.assertFinding(page(body), token)

    def test_a_marker_in_a_script_file_is_a_finding(self):
        self.assertFinding({"site.js": "document.cookie = 'a=b';"}, "document.cookie", "site.js")

    def test_local_storage_a_relative_fetch_and_an_escaped_handler_are_no_finding(self):
        self.assertClean({"theme.js": "localStorage.setItem('theme','dark'); fetch('search-index.json').then(r => r.json());",
                          **page("<p>Use &lt;img onerror=x()&gt; carefully.</p>")})

    def test_private_hosts_and_addresses_are_findings(self):
        for token in ("dev.azure.com", "10.10.3.91", "192.168.1.5", "172.16.0.9",
                      "169.254.1.1"):
            with self.subTest(token=token):
                self.assertFinding(page(f"<p>host {token} here</p>"), token)

    def test_names_passed_in_are_findings_and_the_source_does_not_spell_them(self):
        out = Path(tempfile.mkdtemp())
        (out / "index.html").write_text("<p>see build.example-private.test</p>", encoding="utf-8")
        self.assertTrue(site_checks.check_output(out, "", (), ("example-private.test",)))
        self.assertEqual([], site_checks.check_output(out, "", ()))
        source = (Path(__file__).parent / "site_checks.py").read_text(encoding="utf-8")
        names = Path(__file__).resolve().parents[1] / "docs" / "compliance" / "site-private-names.txt"
        if names.is_file():
            for line in names.read_text(encoding="utf-8").splitlines():
                if line.strip() and not line.startswith("#"):
                    self.assertNotIn(line.strip(), source)

    def test_loopback_and_a_public_address_are_no_finding(self):
        self.assertClean(page("<p>curl http://127.0.0.1:8080 and http://localhost:8080 and 172.32.0.1 and 8.8.8.8</p>"))


# ---------- Review of #3621: bypasses of the output check ----------

class BypassesFoundInReview(Cases):
    def test_a_private_path_after_a_relative_prefix_is_a_finding(self):
        for prefix in ("./", "../", "../../"):
            with self.subTest(prefix=prefix):
                self.assertFinding(page(f"<p>{prefix}docs/feat/1/plan.md</p>"), "docs/feat/1/plan.md")

    def test_requests_made_by_other_attributes_are_findings(self):
        for markup in ('<a href="/p" ping="https://evil.example/t">x</a>', '<video poster="https://evil.example/p.png"></video>',
                       '<form formaction="https://evil.example/s"><button>x</button></form>',
                       '<svg><image href="https://evil.example/i.png"/></svg>',
                       '<svg><image xlink:href="https://evil.example/i.png"/></svg>'):
            with self.subTest(markup=markup):
                self.assertFinding(page(markup), "evil.example")

    def test_a_backslash_after_a_slash_is_another_host(self):
        for url in ("/\\evil.example/x.png", "\\\\evil.example/x.png"):
            with self.subTest(url=url):
                self.assertFinding(page(f'<img src="{url}">'), "evil.example")

    def test_a_greater_than_sign_inside_a_quoted_value_hides_nothing(self):
        self.assertFinding(page('<img alt="a>b" src="https://evil.example/x.png">'), "evil.example")

    def test_a_script_file_with_an_absolute_url_is_a_finding(self):
        self.assertFinding({"site.js": 's=document.createElement("script"); s.src="https://cdn.evil.example/a.js";'}, "evil.example",
                           "site.js")

    def test_a_script_file_with_the_sites_own_address_is_no_finding(self):
        self.assertClean({"site.js": f'var u="https://{HOST}/search-index.json";'})


# ---------- T4: clean, contained, wired into the build ----------

def listing(root: Path, skip: str = "") -> dict[str, tuple[int, int]]:
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(root.rglob("*"))
            if p.is_file() and not p.relative_to(root).as_posix().startswith(skip or "\0")}


class CleanAndContained(Cases):
    def test_a_clean_output_gives_an_empty_list(self):
        self.assertEqual(scan({"index.html": '<html><head><link rel="stylesheet" href="style.css"><script src="theme.js"></script></head>'
                                             '<body><p>Run venom run tests.</p><a href="https://github.com/ovh/venom">gh</a></body></html>',
                               "style.css": "body{color:#000}", "theme.js": "localStorage.getItem('t')"}), [])

    def test_check_output_writes_nothing_anywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            write_files(parent, {"out/a.html": "<p>docs/spec/x.md</p>", "other/keep.txt": "keep"})
            before = listing(parent)
            site_checks.check_output(parent / "out", HOST, PRIVATE)
            self.assertEqual(listing(parent), before)

    def test_a_build_names_the_planted_private_path_and_exits_one(self):
        root = make_repo({"index.md": "# Home\n\nSee docs/spec/x.md for the plan.\n"},
                         [{"slug": "index", "title": "Home", "source": "index.md"}], baseUrl=f"https://{HOST}")
        errors = bs.build(root, root / "_out")
        self.assertTrue(any("index.html" in e and "docs/spec/x.md" in e for e in errors), errors)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = bs.main(["--root", str(root), "--out", str(root / "_out2")])
        self.assertEqual(code, 1, stderr.getvalue())
        shutil.rmtree(root, ignore_errors=True)

    def test_a_clean_repository_builds_with_exit_zero(self):
        root = make_repo({"index.md": "# Home\n\nRun the suite with venom.\n"},
                         [{"slug": "index", "title": "Home", "source": "index.md"}], baseUrl=f"https://{HOST}")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = bs.main(["--root", str(root), "--out", str(root / "_out")])
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        shutil.rmtree(root, ignore_errors=True)

    def test_a_build_writes_nothing_outside_the_output_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root = make_repo({"index.md": "# Home\n\nSee docs/spec/x.md.\n"},
                             [{"slug": "index", "title": "Home", "source": "index.md"}], baseUrl=f"https://{HOST}")
            shutil.copytree(root, parent / "repo")
            shutil.rmtree(root, ignore_errors=True)
            (parent / "out").mkdir()
            before = listing(parent, skip="out/")
            errors = bs.build(parent / "repo", parent / "out")
            self.assertTrue(errors, "the planted private path should make the build report a finding")
            self.assertEqual(listing(parent, skip="out/"), before)

    def test_the_real_repository_builds_with_exit_zero_and_no_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, stderr = build_real(Path(tmp))
            self.assertEqual((code, stderr), (0, ""), "the real site must pass the output checks")


# ---------- T5: make targets and .gitignore ----------

def make_text() -> str:
    return (ROOT / "Makefile").read_text(encoding="utf-8")


def phony_names(text: str) -> set[str]:
    names: set[str] = set()
    for match in re.finditer(r"^\.PHONY:(.*(?:\\\n.*)*)", text, re.M):
        names.update(match.group(1).replace("\\\n", " ").split())
    return names


def recipe_of(text: str, target: str) -> str:
    """The target's line and its tab-indented recipe lines."""
    match = re.search(rf"^{re.escape(target)}:[^\n]*(?:\n\t[^\n]*)*", text, re.M)
    return match.group(0) if match else ""


class MakeAndGit(unittest.TestCase):
    def test_site_and_site_check_are_phony_targets(self):
        self.assertTrue({"site", "site-check"} <= phony_names(make_text()), phony_names(make_text()))

    def test_each_target_has_a_help_comment(self):
        text = make_text()
        for target in ("site", "site-check"):
            with self.subTest(target=target):
                self.assertRegex(text, rf"(?m)^{re.escape(target)}:[^\n#]*##\s*\S", f"{target} needs a `##` help comment")

    def test_site_builds_into_build_site_with_the_build_script(self):
        self.assertIn("scripts/build_site.py --out build/site", recipe_of(make_text(), "site"))
        self.assertRegex(make_text(), r"(?m)^PYTHON\s*\?=\s*python3")

    def test_site_check_runs_site_then_the_example_check(self):
        recipe = recipe_of(make_text(), "site-check")
        self.assertIn("scripts/check-site-example.sh", recipe)
        self.assertTrue(re.search(r"^site-check:[^\n#]*\bsite\b", recipe) or re.search(r"\$\(MAKE\)\s+site\b", recipe), recipe)

    def test_gitignore_ignores_the_build_folder(self):
        rules = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(rule in ("build/", "/build/", "build", "/build") for rule in rules), "no rule ignores build/")

    def test_make_dry_run_prints_the_build_command(self):
        make = shutil.which("make")
        if not make:
            self.skipTest("make is not on PATH")
        result = subprocess.run([make, "-n", "site"], cwd=ROOT, capture_output=True, text=True, timeout=300)
        self.assertIn("scripts/build_site.py --out build/site", result.stdout, result.stderr[:300])


# ---------- T6: the mobile README points at public files ----------

MOBILE = ROOT / "executors" / "mobile"
README_PATH = re.compile(r"(?<![\w./-])((?:executors/mobile/|examples/)[\w./-]*[\w/])")


class MobileReadme(unittest.TestCase):
    def readme(self) -> str:
        return (MOBILE / "README.md").read_text(encoding="utf-8")

    def test_every_repository_path_the_readme_names_exists(self):
        paths = sorted(set(README_PATH.findall(self.readme())))
        self.assertTrue(paths, "the README should name its example files")
        for path in paths:
            with self.subTest(path=path):
                target = ROOT / path if path.startswith("executors/") else MOBILE / path
                self.assertTrue(target.exists(), f"README names {path}, which does not exist")

    def test_the_readme_names_no_path_starting_with_docs(self):
        self.assertEqual(re.findall(r"(?<![\w./-])docs/[\w./-]+", self.readme()), [])

    def test_the_sample_files_are_published_under_examples(self):
        for name in EXAMPLES:
            with self.subTest(file=name):
                self.assertTrue((MOBILE / "examples" / name).is_file(), f"executors/mobile/examples/{name} does not exist")

    def test_the_readme_points_at_each_published_sample(self):
        for name in EXAMPLES:
            with self.subTest(file=name):
                self.assertIn(f"examples/{name}", self.readme())

    def test_no_sample_file_holds_a_private_looking_value(self):
        for name in EXAMPLES:
            path = MOBILE / "examples" / name
            with self.subTest(file=name):
                if not path.is_file():
                    self.fail(f"executors/mobile/examples/{name} does not exist")
                text = path.read_text(encoding="utf-8")
                for token in ("dev.azure.com", "botbuilder", "id_ed25519", "/Users/"):
                    self.assertNotIn(token, text)
                self.assertIsNone(PRIVATE_ADDRESS.search(text))


if __name__ == "__main__":
    unittest.main()
