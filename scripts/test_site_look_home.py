#!/usr/bin/env python3
"""Tests of the poster home page and of the removal of the old look (story #3667, task #3673).
They build the REAL repository. Offline, standard library only. Run: python scripts/test_site_look_home.py
"""
import functools
import json
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs  # noqa: E402
from site_checks import PRIVATE_HOSTS, _private_path_regex  # noqa: E402
from test_build_site import ROOT  # noqa: E402
from test_site_quickstart import build_real, sidebar_groups  # noqa: E402

OLD_NAME = "cra" + "ticula"
RUN_OUTPUT = ROOT / "site" / "examples" / "run-output.txt"
SUITE = ROOT / "site" / "examples" / "quickstart.yml"
RUN_COMMAND = "venom run site/examples/quickstart.yml"
BUILD_COMMAND = "go build -o venom ./cmd/venom"
NOTICE = "Pluries Venom is an independent fork of ovh/venom, published under the Apache-2.0 licence"
UPSTREAM_URL = "https://github.com/ovh/venom"
VOID = {"meta", "link", "br", "img", "hr", "input", "source", "area", "base", "col", "embed", "wbr", "track", "param"}
TEXT_SUFFIXES = {".md", ".html", ".css", ".js", ".svg", ".json", ".txt", ".yml", ".yaml"}


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class Walker(HTMLParser):
    """Collects the text of the elements `wants(tag, classes, ancestors)` accepts; ancestors are the open (tag, classes) pairs."""

    def __init__(self, wants) -> None:
        super().__init__()
        self.wants, self.stack, self.open, self.done = wants, [], [], []

    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        classes = set((dict(attrs).get("class") or "").split())
        if self.wants(tag, classes, self.stack):
            self.open.append([len(self.stack), []])
        self.stack.append((tag, classes))

    def handle_endtag(self, tag):
        if tag in VOID or not any(t == tag for t, _ in self.stack):
            return
        while self.stack and self.stack.pop()[0] != tag:
            pass
        while self.open and self.open[-1][0] >= len(self.stack):
            self.done.append("".join(self.open.pop()[1]))

    def handle_data(self, data):
        for entry in self.open:
            entry[1].append(data)


def collect(page_html: str, wants) -> list[str]:
    walker = Walker(wants)
    walker.feed(page_html)
    walker.close()
    return walker.done


def by_class(page_html: str, name: str) -> list[str]:
    return collect(page_html, lambda tag, classes, anc: name in classes)


def titles_of(page_html: str) -> list[str]:
    return [squash(t) for t in collect(page_html, lambda tag, classes, anc: tag == "title")]


class FooterLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_footer, self.hrefs, self.text = False, [], []

    def handle_starttag(self, tag, attrs):
        if tag == "footer":
            self.in_footer = True
        if self.in_footer and tag == "a":
            self.hrefs.append(dict(attrs).get("href", ""))

    def handle_endtag(self, tag):
        if tag == "footer":
            self.in_footer = False

    def handle_data(self, data):
        if self.in_footer:
            self.text.append(data)


def footer_problems(page_html: str) -> list[str]:
    """What the footer of one built page lacks of the fork notice (empty when complete)."""
    parser = FooterLinks()
    parser.feed(page_html)
    problems = []
    if NOTICE not in squash("".join(parser.text)):
        problems.append("the fork notice is missing")
    if UPSTREAM_URL not in parser.hrefs:
        problems.append("no link to upstream")
    if not any(h.endswith("/LICENSE") for h in parser.hrefs):
        problems.append("no link to the licence")
    return problems


def old_name_files(root: Path, files: list[Path]) -> list[str]:
    """The files (relative to root) whose text names the old project."""
    return [str(p.relative_to(root)) for p in files if OLD_NAME in p.read_text(encoding="utf-8", errors="replace").lower()]


@functools.lru_cache(maxsize=None)
def _built() -> Path:
    out = Path(tempfile.mkdtemp())
    code, stdout, stderr = build_real(out)
    if code != 0:
        raise AssertionError(f"the real build failed: {stdout}{stderr}")
    return out


def executor_count() -> int:
    return sum(1 for p in (ROOT / "executors").iterdir() if p.is_dir() and p.name != "plugins" and (p / "README.md").is_file())


class Real(unittest.TestCase):
    def out(self) -> Path:
        return _built()

    def home(self) -> str:
        return (self.out() / "index.html").read_text(encoding="utf-8")

    def pages(self) -> dict[str, str]:
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(self.out().glob("*.html"))}

    def run_lines(self) -> list[str]:
        self.assertTrue(RUN_OUTPUT.is_file(), "site/examples/run-output.txt does not exist")
        return RUN_OUTPUT.read_text(encoding="utf-8").splitlines()


class Home(Real):
    def test_the_home_page_has_a_hero_and_the_poster_headline(self):
        page = self.home()
        self.assertTrue(by_class(page, "hero"), "no element with the class hero")
        headlines = [squash(t) for t in collect(page, lambda tag, classes, anc: tag == "h1")]
        self.assertEqual(headlines, ["YAML in. PASS out."])

    def test_the_run_output_panel_shows_the_command_then_the_captured_lines(self):
        lines = [squash(line) for line in self.run_lines()]
        page = self.home()
        panels = [squash(t) for t in by_class(page, "demo") + by_class(page, "panel")]
        shown = [p for p in panels if RUN_COMMAND in p]
        self.assertTrue(shown, f"no .demo or .panel shows `{RUN_COMMAND}`")
        after = shown[0][shown[0].index(RUN_COMMAND) + len(RUN_COMMAND):]
        for line in lines:
            with self.subTest(line=line):
                self.assertIn(line, after)

    def test_the_home_page_links_to_the_quickstart_and_the_first_page_of_every_group(self):
        page = self.home()
        self.assertIn('href="quickstart.html"', page)
        for name, hrefs in sidebar_groups(page).items():
            if hrefs:
                with self.subTest(group=name):
                    self.assertIn(f'href="{hrefs[0]}"', page)

    def test_the_build_command_sits_in_a_prose_pre_so_it_gets_a_copy_button(self):
        pres = collect(self.home(), lambda tag, classes, anc: tag == "pre" and any("prose" in c for _, c in anc))
        self.assertTrue(any(BUILD_COMMAND in squash(p) for p in pres), pres)

    def test_the_facts_row_has_four_facts_and_the_executor_count_is_the_real_one(self):
        page = self.home()
        self.assertTrue(by_class(page, "facts"), "no .facts row")
        facts = [squash(t) for t in by_class(page, "fact")]
        self.assertEqual(len(facts), 4, facts)
        counts = [int(m.group(1)) for f in facts for m in [re.search(r"(\d+)\s*executors", f)] if m]
        self.assertEqual(counts, [executor_count()], facts)


def panel_lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.replace("\r\n", "\n").rstrip().split("\n")]


def panel_problems(panel_text: str, source_text: str) -> list[str]:
    """Where the panel text differs from the source file, line by line (empty when verbatim)."""
    shown, wanted = panel_lines(panel_text), panel_lines(source_text)
    problems = [f"line {i + 1}: {a!r} != {b!r}" for i, (a, b) in enumerate(zip(shown, wanted)) if a != b]
    if len(shown) != len(wanted):
        problems.append(f"{len(shown)} lines shown, {len(wanted)} in the file")
    return problems


class YamlPanel(Real):
    def test_the_first_demo_panel_shows_quickstart_yml_verbatim(self):
        pres = collect(self.home(), lambda tag, classes, anc: tag == "pre"
                       and any("demo" in c for _, c in anc) and any("panel" in c for _, c in anc))
        self.assertTrue(pres, "no pre in a .panel of the .demo")
        self.assertEqual(panel_problems(pres[0], SUITE.read_text(encoding="utf-8")), [])

    def test_the_panel_checker_reports_a_different_text_and_accepts_the_same(self):
        source = "name: a\ntestcases:\n  - name: b\n"
        self.assertEqual(panel_problems("name: a\r\ntestcases:  \n  - name: b\n\n", source), [])
        self.assertEqual(len(panel_problems("name: a\ntestcases:\n  - name: c\n", source)), 1)
        self.assertEqual(len(panel_problems("name: a\ntestcases:\n", source)), 1)
        self.assertEqual(len(panel_problems("name: a\ntestcases:\n- name: b\n", source)), 1)


class RunOutput(Real):
    def test_one_pass_line_per_test_case_then_the_final_status(self):
        lines = self.run_lines()
        cases = len(re.findall(r"^\s*- name:", SUITE.read_text(encoding="utf-8").split("testcases:", 1)[1], re.M))
        self.assertEqual(sum(1 for line in lines if line.rstrip().endswith("PASS")), cases + 1, lines)
        self.assertEqual(lines[-1], "final status: PASS")

    def test_the_captured_file_leaks_nothing(self):
        text = "\n".join(self.run_lines())
        self.assertNotIn("\x1b", text)
        self.assertNotIn("venom.1.log", text)
        self.assertEqual(PRIVATE_HOSTS.findall(text), [])
        self.assertEqual(re.findall(r"[A-Za-z]:[\\/]|(?<![\w.])/(?:home|tmp|usr|var|Users)/\S*", text), [])
        self.assertEqual([p for p in re.findall(r"[\w./-]+/[\w./-]+", text) if not p.startswith("site/examples/")], [])
        self.assertEqual(_private_path_regex(bs.PRIVATE_PATHS).findall(text), [])
        self.assertEqual(re.findall(r"(?:https?:)?//\S+", text), [])


class Footer(Real):
    def test_every_built_page_carries_the_notice_and_the_links(self):
        pages = self.pages()
        self.assertIn("index.html", pages)
        for name, page in pages.items():
            with self.subTest(page=name):
                self.assertEqual(footer_problems(page), [])

    def test_a_page_without_the_footer_is_reported(self):
        self.assertEqual(len(footer_problems('<html><body><footer><a href="x">x</a></footer></body></html>')), 3)
        self.assertEqual(len(footer_problems("<html><body><p>x</p></body></html>")), 3)

    def test_no_ovh_logo_file_is_published_under_site(self):
        found = [str(p.relative_to(ROOT)) for p in (ROOT / "site").rglob("*") if p.is_file() and "ovh" in p.name.lower()]
        self.assertEqual(found, [])


class OldName(Real):
    def test_no_source_text_file_under_site_names_the_old_project(self):
        files = [p for p in (ROOT / "site").rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES]
        self.assertTrue(files)
        self.assertEqual(old_name_files(ROOT, files), [])

    def test_no_built_file_names_the_old_project(self):
        out = self.out()
        files = [p for p in out.rglob("*") if p.is_file() and p.suffix in (".html", ".css", ".js", ".svg", ".json")]
        self.assertTrue(files)
        self.assertEqual(old_name_files(out, files), [])

    def test_the_site_scripts_do_not_name_the_old_project(self):
        files = [p for p in (ROOT / "scripts").glob("*site*.py") if not p.name.startswith("test_")]
        self.assertTrue(files)
        self.assertEqual(old_name_files(ROOT, files), [])

    def test_a_planted_occurrence_is_reported(self):
        root = Path(tempfile.mkdtemp())
        planted, clean = root / "a.html", root / "b.html"
        planted.write_text(f"<p>From {OLD_NAME.upper()}</p>", encoding="utf-8")
        clean.write_text("<p>Pluries Venom</p>", encoding="utf-8")
        self.assertEqual(old_name_files(root, [planted, clean]), ["a.html"])


class Titles(Real):
    def test_every_docs_page_title_ends_with_the_product_name(self):
        for name, page in self.pages().items():
            if name == "index.html":
                continue
            with self.subTest(page=name):
                found = titles_of(page)
                self.assertEqual(len(found), 1)
                self.assertTrue(found[0].endswith(" · Pluries Venom"), found)

    def test_the_home_title_starts_with_the_product_name(self):
        self.assertEqual(len(titles_of(self.home())), 1)
        self.assertTrue(titles_of(self.home())[0].startswith("Pluries Venom"), titles_of(self.home()))

    def test_the_home_title_names_the_product_exactly_once(self):
        self.assertEqual(titles_of(self.home())[0].count("Pluries Venom"), 1, titles_of(self.home()))

    def test_the_docs_page_titles_read_page_dot_product(self):
        self.assertEqual(titles_of(self.pages()["quickstart.html"]), ["Quickstart · Pluries Venom"])

    def test_the_site_config_title_is_the_product_name(self):
        self.assertEqual(json.loads((ROOT / "site" / "site.json").read_text(encoding="utf-8"))["title"], "Pluries Venom")

    def test_a_site_name_meta_says_the_product_name(self):
        for name, page in self.pages().items():
            for content in re.findall(r"""<meta[^>]+(?:og:site_name|application-name)[^>]*content=["']([^"']*)""", page):
                with self.subTest(page=name):
                    self.assertEqual(content, "Pluries Venom")


if __name__ == "__main__":
    unittest.main()
