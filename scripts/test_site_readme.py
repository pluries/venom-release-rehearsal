#!/usr/bin/env python3
"""Tests of scripts/site_readme_split.py and of the `readme-split` page entry of scripts/build_site.py (story #3621, task #3636).
Offline, standard library only. Run: python scripts/test_site_readme.py
"""
import contextlib
import io
import json
import re
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs  # noqa: E402
import site_executors  # noqa: E402,F401
import site_readme_split as split  # noqa: E402
from test_build_site import INDEX, REPO, ROOT, TEMPLATE, write_files  # noqa: E402

FIXTURE = """# Title

Intro line.

<a href="https://shields.io/x"><img alt="Release" src="https://img.shields.io/r.svg"></a>
[![GoDoc](https://godoc.org/x?status.svg)](https://godoc.org/x)
[![Card](https://goreportcard.com/badge/x)](https://goreportcard.com/report/x)

# Table of contents

- [Alpha](#alpha)
- [Beta](#beta)

# Alpha

Alpha text.

## Alpha sub

Alpha sub text.

# Beta

Beta text.

```sh
# a comment that looks like a heading
echo hi
```

# Gamma

Gamma text.
"""
SKIP = ["Table of contents", "Title"]


def item(heading: str, slug: str, group: str = "Guides", **extra) -> dict:
    return {"heading": heading, "slug": slug, "group": group, "description": f"About {heading}.", **extra}


SECTIONS = [item("Alpha", "alpha", "Getting started"), item("Beta", "beta"), item("Gamma", "gamma", "Reference")]


def entry(sections: list[dict] = None, skip: list[str] = None) -> dict:
    return {"generator": "readme-split", "source": "README.md", "skip": SKIP if skip is None else skip,
            "sections": SECTIONS if sections is None else sections}


def scratch(files: dict[str, str], pages: list[dict]) -> Path:
    root = Path(tempfile.mkdtemp())
    config = {"title": "T", "repo": REPO, "branch": "main", "baseUrl": "https://docs.example.test", "pages": pages}
    write_files(root, {"site/site.json": json.dumps(config), "site/template.html": TEMPLATE, "site/style.css": "body{}",
                       "index.md": "# Home\n", **files})
    return root


def split_repo(readme: str, sections: list[dict] = None, skip: list[str] = None, files: dict[str, str] = None,
               extra_pages: list[dict] = ()) -> Path:
    pages = [INDEX, entry(sections, skip), *extra_pages]
    return scratch({"README.md": readme, **(files or {})}, pages)


def build_html(readme: str, sections: list[dict] = None, skip: list[str] = None, files: dict[str, str] = None,
               extra_pages: list[dict] = ()):
    root = split_repo(readme, sections, skip, files, extra_pages)
    out = root / "_out"
    errors = bs.build(root, out)
    return errors, out, root


def page_html(out: Path, slug: str) -> str:
    return (out / f"{slug}.html").read_text(encoding="utf-8")


def by_slug(pages: list[dict]) -> dict[str, dict]:
    return {p["slug"]: p for p in pages}


def lines_of(text: str) -> list[str]:
    return [line.rstrip() for line in text.split("\n") if line.strip()]


class T1PagesFromSections(unittest.TestCase):
    def setUp(self):
        self.root = split_repo(FIXTURE)
        self.pages = split.expand(self.root, entry())

    def test_three_sections_become_three_pages_in_order_with_group_and_description(self):
        self.assertEqual([(p["slug"], p["title"], p["group"], p["description"], p["source"]) for p in self.pages],
                         [("alpha", "Alpha", "Getting started", "About Alpha.", "README.md"),
                          ("beta", "Beta", "Guides", "About Beta.", "README.md"),
                          ("gamma", "Gamma", "Reference", "About Gamma.", "README.md")])

    def test_a_page_text_is_its_section_with_the_heading_kept(self):
        self.assertEqual(by_slug(self.pages)["alpha"]["text"].strip(),
                         "# Alpha\n\nAlpha text.\n\n## Alpha sub\n\nAlpha sub text.")
        self.assertEqual(by_slug(self.pages)["gamma"]["text"].strip(), "# Gamma\n\nGamma text.")

    def test_a_hash_line_in_a_code_fence_is_not_a_heading_and_stays_in_its_page(self):
        text = by_slug(self.pages)["beta"]["text"]
        self.assertEqual(len(self.pages), 3)
        self.assertIn("# a comment that looks like a heading\necho hi\n```", text)

    def test_no_line_of_a_kept_section_is_lost_or_duplicated_and_the_skipped_parts_are_gone(self):
        joined = [line for p in self.pages for line in lines_of(p["text"])]
        expected = lines_of(FIXTURE.split("# Alpha\n", 1)[1])
        self.assertEqual(joined, ["# Alpha"] + expected)
        for dropped in ("Title", "Intro line.", "godoc.org", "Table of contents", "- [Alpha](#alpha)"):
            self.assertFalse(any(dropped in line for line in joined), dropped)

    def test_the_title_defaults_to_the_heading_and_can_be_overridden(self):
        sections = [item("Alpha", "alpha", title="The alpha"), item("Beta", "beta"), item("Gamma", "gamma")]
        pages = split.expand(self.root, entry(sections))
        self.assertEqual([p["title"] for p in pages], ["The alpha", "Beta", "Gamma"])

    def test_a_section_is_addressed_by_its_heading_text_without_surrounding_spaces(self):
        root = split_repo("# Alpha \n\nA.\n", [item("Alpha", "alpha")], skip=[])
        self.assertEqual(split.expand(root, entry([item("Alpha", "alpha")], skip=[]))[0]["title"], "Alpha")

    def test_an_h1_that_is_neither_listed_nor_skipped_is_an_error_naming_it(self):
        with self.assertRaises(split.SplitError) as caught:
            split.expand(self.root, entry(SECTIONS[:2]))
        self.assertIn("README.md section 'Gamma' has no page and is not skipped", str(caught.exception))

    def test_the_build_reports_the_uncovered_section_as_a_build_error(self):
        root = split_repo(FIXTURE, SECTIONS[:2])
        with self.assertRaises(bs.BuildError) as caught:
            bs.load_config(root)
        self.assertIn("section 'Gamma' has no page and is not skipped", str(caught.exception))

    def test_a_section_heading_that_is_not_in_the_readme_is_an_error_naming_it(self):
        with self.assertRaises(split.SplitError) as caught:
            split.expand(self.root, entry([*SECTIONS, item("Nope", "nope")]))
        self.assertIn("'Nope'", str(caught.exception))

    def test_a_skip_heading_that_is_not_in_the_readme_is_an_error_naming_it(self):
        with self.assertRaises(split.SplitError) as caught:
            split.expand(self.root, entry(skip=[*SKIP, "Ghost"]))
        self.assertIn("'Ghost'", str(caught.exception))

    def test_the_build_reports_a_missing_heading_as_a_build_error(self):
        root = split_repo(FIXTURE, [*SECTIONS, item("Nope", "nope")])
        with self.assertRaises(bs.BuildError) as caught:
            bs.load_config(root)
        self.assertIn("'Nope'", str(caught.exception))

    def test_the_build_renders_the_page_text_and_points_edit_on_github_at_the_readme(self):
        errors, out, _ = build_html(FIXTURE)
        self.assertEqual(errors, [])
        html_text = page_html(out, "alpha")
        self.assertIn("Alpha sub text.", html_text)
        self.assertIn(f"{REPO}/blob/main/README.md", html_text)
        self.assertNotIn("Intro line.", html_text)
        self.assertNotIn("Gamma text.", html_text)


ANCHORS = """# Alpha

See [beta](#beta-section), [first variables](#variables), [second variables](#variables-1).

## Variables

Alpha variables.

# Beta section

Mine: [the second variables](#variables-1), and the first: [variables](#variables).

## Variables

Beta variables.
"""
ANCHOR_SECTIONS = [item("Alpha", "alpha"), item("Beta section", "beta")]


class T2Anchors(unittest.TestCase):
    def test_an_anchor_on_another_page_becomes_that_page_with_its_own_anchor(self):
        errors, out, _ = build_html(ANCHORS, ANCHOR_SECTIONS, skip=[])
        self.assertEqual(errors, [])
        alpha = page_html(out, "alpha")
        self.assertIn('href="beta.html#beta-section"', alpha)
        self.assertIn('href="beta.html#variables"', alpha)  # README's #variables-1 is the first Variables of the page beta

    def test_an_anchor_on_the_same_page_stays_in_page(self):
        errors, out, _ = build_html(ANCHORS, ANCHOR_SECTIONS, skip=[])
        self.assertEqual(errors, [])
        self.assertIn('href="#variables"', page_html(out, "alpha"))

    def test_a_repeated_heading_is_numbered_over_the_whole_readme_but_per_page_on_the_page(self):
        errors, out, _ = build_html(ANCHORS, ANCHOR_SECTIONS, skip=[])
        self.assertEqual(errors, [])
        beta = page_html(out, "beta")
        self.assertIn('href="#variables"', beta)  # README's #variables-1 is on this very page, where it is numbered #variables
        self.assertIn('href="alpha.html#variables"', beta)  # README's #variables is the first one, on the page alpha

    def test_an_anchor_that_exists_nowhere_in_the_readme_is_a_build_error_naming_it(self):
        readme = ANCHORS.replace("[beta](#beta-section)", "[gone](#nowhere)")
        errors, _, _ = build_html(readme, ANCHOR_SECTIONS, skip=[])
        self.assertTrue(any("nowhere" in e for e in errors), errors)

    def test_an_anchor_that_exists_nowhere_is_also_refused_on_a_single_page_readme(self):
        errors, _, _ = build_html("# Alpha\n\n[gone](#nowhere)\n", [item("Alpha", "alpha")], skip=[])
        self.assertTrue(any("nowhere" in e for e in errors), errors)


class T3BadgeBlock(unittest.TestCase):
    def test_the_badge_block_with_third_party_images_reaches_no_output_file(self):
        errors, out, _ = build_html(FIXTURE)
        self.assertEqual(errors, [])
        files = [p for p in out.rglob("*") if p.is_file()]
        self.assertGreater(len(files), 3)
        for path in files:
            data = path.read_bytes()
            for host in (b"shields.io", b"godoc.org", b"goreportcard.com", b"GoDoc", b"Go Report Card"):
                self.assertNotIn(host, data, f"{host!r} in {path.name}")

    def test_a_readme_without_a_badge_block_builds_unchanged(self):
        readme = "# Title\n\n# Alpha\n\nAlpha text with `code`.\n\n# Beta\n\nBeta text.\n"
        sections = [item("Alpha", "alpha"), item("Beta", "beta")]
        pages = split.expand(split_repo(readme, sections, ["Title"]), entry(sections, ["Title"]))
        self.assertEqual([p["text"].strip() for p in pages],
                         ["# Alpha\n\nAlpha text with `code`.", "# Beta\n\nBeta text."])
        errors, out, _ = build_html(readme, sections, ["Title"])
        self.assertEqual(errors, [])
        self.assertIn("<code>code</code>", page_html(out, "alpha"))

    def test_a_remote_image_written_as_html_in_a_kept_section_is_dropped_to_its_alt_text(self):
        readme = '# Alpha\n\n<a href="https://example.test/x"><img src="https://img.shields.io/a.svg" alt="Contribute"/></a>\n'
        errors, out, _ = build_html(readme, [item("Alpha", "alpha")], skip=[])
        self.assertEqual(errors, [])
        data = page_html(out, "alpha")
        self.assertNotIn("shields.io", data)
        self.assertNotIn("<img", data)


class T4RepositoryLinks(unittest.TestCase):
    LINKS = ("# Alpha\n\nSee [x](/tests/x.yml), [c](./CONTRIBUTING.md) and [w](executors/web/README.md).\n")
    FILES = {"tests/x.yml": "name: x\n", "CONTRIBUTING.md": "# Contributing\n", "executors/web/README.md": "# Venom - Executor Web\n"}
    EXECUTORS = {"generator": "executors", "group": "Executors"}

    def test_a_root_relative_link_to_an_existing_file_is_the_github_address_of_the_file(self):
        errors, out, _ = build_html(self.LINKS, [item("Alpha", "alpha")], skip=[], files=self.FILES)
        self.assertEqual(errors, [])
        self.assertIn(f'href="{REPO}/blob/main/tests/x.yml"', page_html(out, "alpha"))

    def test_a_dot_slash_link_to_a_file_without_a_page_is_its_github_address(self):
        errors, out, _ = build_html(self.LINKS, [item("Alpha", "alpha")], skip=[], files=self.FILES)
        self.assertEqual(errors, [])
        self.assertIn(f'href="{REPO}/blob/main/CONTRIBUTING.md"', page_html(out, "alpha"))

    def test_a_link_to_a_file_that_is_a_page_of_the_site_is_that_page(self):
        page = {"slug": "contributing", "title": "Contributing", "source": "CONTRIBUTING.md", "group": "Reference", "description": "How."}
        errors, out, _ = build_html(self.LINKS, [item("Alpha", "alpha")], skip=[], files=self.FILES, extra_pages=[page])
        self.assertEqual(errors, [])
        self.assertIn('href="contributing.html"', page_html(out, "alpha"))

    def test_a_link_to_another_executor_readme_is_that_page_when_the_site_has_the_executors_generator(self):
        errors, out, _ = build_html(self.LINKS, [item("Alpha", "alpha")], skip=[], files=self.FILES, extra_pages=[self.EXECUTORS])
        self.assertEqual(errors, [])
        self.assertIn('href="web.html"', page_html(out, "alpha"))

    def test_a_root_relative_link_to_a_missing_file_is_a_build_error_naming_the_link(self):
        readme = "# Alpha\n\nSee [gone](/tests/gone.yml).\n"
        errors, _, _ = build_html(readme, [item("Alpha", "alpha")], skip=[], files=self.FILES)
        self.assertEqual(len(errors), 1)
        self.assertIn("/tests/gone.yml", errors[0])

    def test_a_dot_slash_link_to_a_missing_file_is_a_build_error_naming_the_link(self):
        readme = "# Alpha\n\nSee [gone](./GONE.md).\n"
        errors, _, _ = build_html(readme, [item("Alpha", "alpha")], skip=[], files=self.FILES)
        self.assertEqual(len(errors), 1)
        self.assertIn("./GONE.md", errors[0])

    def test_a_root_relative_link_into_the_private_part_is_a_build_error(self):
        readme = "# Alpha\n\nSee [secret](/docs/secret.md).\n"
        errors, _, _ = build_html(readme, [item("Alpha", "alpha")], skip=[], files={"docs/secret.md": "x\n"})
        self.assertEqual(len(errors), 1)
        self.assertIn("/docs/secret.md", errors[0])


CONCEPTS = """# Intro

Intro text.

# Concepts

Concepts own text.

## Suites

Suites text.

### Suites detail

Detail text.

## Misc

Misc text, not split out.

## Vars

Vars text.

# After

After text.
"""
SPLIT = [item("Suites", "suites", "Concepts"), item("Vars", "vars", "Reference", title="Variables")]
CONCEPTS_SECTIONS = [item("Intro", "intro", "Getting started"), item("Concepts", "concepts", "Concepts", split=SPLIT),
                     item("After", "after")]


class T5SplitSection(unittest.TestCase):
    def setUp(self):
        self.root = split_repo(CONCEPTS, CONCEPTS_SECTIONS, skip=[])
        self.pages = split.expand(self.root, entry(CONCEPTS_SECTIONS, skip=[]))

    def test_a_split_section_gives_the_parent_first_then_one_page_per_listed_h2(self):
        self.assertEqual([(p["slug"], p["title"], p["group"]) for p in self.pages],
                         [("intro", "Intro", "Getting started"), ("concepts", "Concepts", "Concepts"),
                          ("suites", "Suites", "Concepts"), ("vars", "Variables", "Reference"), ("after", "After", "Guides")])

    def test_the_parent_keeps_its_own_text_and_the_h2_that_is_not_listed(self):
        self.assertEqual(by_slug(self.pages)["concepts"]["text"].strip(),
                         "# Concepts\n\nConcepts own text.\n\n## Misc\n\nMisc text, not split out.")

    def test_a_split_out_h2_is_promoted_to_the_pages_h1_and_keeps_its_deeper_headings(self):
        self.assertEqual(by_slug(self.pages)["suites"]["text"].strip(), "# Suites\n\nSuites text.\n\n### Suites detail\n\nDetail text.")
        self.assertEqual(by_slug(self.pages)["vars"]["text"].strip(), "# Vars\n\nVars text.")

    def test_a_split_never_loses_or_duplicates_a_line(self):
        joined = Counter(line for p in self.pages for line in lines_of(p["text"]))
        expected = Counter(re.sub(r"^## (Suites|Vars)$", r"# \1", line) for line in lines_of(CONCEPTS))
        self.assertEqual(joined, expected)

    def test_a_parent_left_with_only_its_heading_is_dropped(self):
        readme = CONCEPTS.replace("Concepts own text.\n\n", "")
        sections = [item("Concepts", "concepts", "Concepts", split=[*SPLIT, item("Misc", "misc", "Concepts")]), item("Intro", "intro"), item("After", "after")]
        pages = split.expand(split_repo(readme, sections, skip=[]), entry(sections, skip=[]))
        self.assertNotIn("concepts", [p["slug"] for p in pages])
        self.assertEqual({"suites", "misc", "vars"} <= {p["slug"] for p in pages}, True)

    def test_a_parent_whose_h2_are_all_split_out_keeps_its_own_text(self):
        sections = [item("Concepts", "concepts", "Concepts", split=[*SPLIT, item("Misc", "misc", "Concepts")]), item("Intro", "intro"), item("After", "after")]
        pages = split.expand(split_repo(CONCEPTS, sections, skip=[]), entry(sections, skip=[]))
        self.assertIn("concepts", [p["slug"] for p in pages])
        self.assertEqual(by_slug(pages)["concepts"]["text"].strip(), "# Concepts\n\nConcepts own text.")

    def test_a_split_heading_that_is_not_an_h2_of_the_section_is_an_error_naming_it(self):
        sections = [item("Intro", "intro"), item("Concepts", "concepts", split=[item("After", "x")]), item("After", "after")]
        with self.assertRaises(split.SplitError) as caught:
            split.expand(self.root, entry(sections, skip=[]))
        self.assertIn("'After'", str(caught.exception))

    def test_the_build_gives_each_split_page_its_own_html_file(self):
        errors, out, _ = build_html(CONCEPTS, CONCEPTS_SECTIONS, skip=[])
        self.assertEqual(errors, [])
        self.assertIn("Suites text.", page_html(out, "suites"))
        self.assertNotIn("Suites text.", page_html(out, "concepts"))
        self.assertIn("Misc text, not split out.", page_html(out, "concepts"))


def h1_sections(readme: str) -> list[tuple[str, list[str]]]:
    """(heading text, its lines including the heading) per `# ` heading outside code fences."""
    sections: list[tuple[str, list[str]]] = []
    fence = None
    for line in readme.split("\n"):
        marker = re.match(r"\s*(```+|~~~+)", line)
        if marker:
            fence = None if fence and marker.group(1)[0] == fence[0] else (fence or marker.group(1))
        heading = None if fence or marker else re.match(r"#\s+(.*?)\s*$", line)
        if heading:
            sections.append((heading.group(1), []))
        if sections:
            sections[-1][1].append(line)
    return sections


REAL_MAP = [("Overview", "overview", "Getting started"), ("Installing", "installing", "Getting started"),
            ("Updating", "updating", "Getting started"), ("Docker image", "docker", "Getting started"),
            ("Write and run your first test suite", "first-test", "Getting started"), ("CLI Usage", "cli-usage", "Guides"),
            ("Export tests report", "reports", "Guides"), ("Advanced usage", "advanced", "Guides"), ("FAQ", "faq", "Guides"),
            ("Use venom in CI/CD pipelines", "ci-cd", "Guides"), ("Hacking", "hacking", "Reference"),
            ("Contributing", "contributing", "Reference"), ("License", "license", "Reference")]
REAL_SPLIT = [("TestSuites", "testsuites", "Concepts"), ("Executors", "executors-concept", "Concepts"),
              ("Variables", "variables", "Concepts"), ("Use outputs from a test step as input of another test step", "step-outputs", "Concepts"),
              ("Builtin venom variables", "builtin-variables", "Concepts"), ("Assertions", "assertions", "Reference")]


def real_item(row: tuple) -> dict:
    return item(row[0], row[1], row[2])


class T6RealReadme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.title = h1_sections(cls.readme)[0][0]
        sections = [real_item(r) for r in REAL_MAP]
        sections.insert(5, item("Concepts", "concepts", "Concepts", split=[real_item(r) for r in REAL_SPLIT]))
        cls.entry = entry(sections, skip=["Table of contents", cls.title])
        cls.root = scratch({"README.md": cls.readme}, [INDEX, cls.entry])
        shutil.copy(ROOT / "CONTRIBUTING.md", cls.root / "CONTRIBUTING.md")
        shutil.copy(ROOT / "venom.gif", cls.root / "venom.gif")
        for target in re.findall(r"\]\(\s*<?([^)\s>]+)", cls.readme) + re.findall(r'src="([^"]+)"', cls.readme):
            path = target.split("#")[0].lstrip("/").removeprefix("./")
            if not path or re.match(r"[a-z][a-z0-9+.-]*:", target) or target.startswith("#") or (cls.root / path).exists():
                continue
            (cls.root / path).parent.mkdir(parents=True, exist_ok=True)
            (cls.root / path).write_text("", encoding="utf-8")
        cls.out = cls.root / "_out"
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
            cls.code = bs.main(["--root", str(cls.root), "--out", str(cls.out)])
        cls.stderr = err.getvalue()

    def test_the_build_exits_zero(self):
        self.assertEqual(self.code, 0, self.stderr)

    def test_every_non_skipped_line_of_the_readme_is_on_exactly_one_page(self):
        pages = bs.load_config(self.root)["pages"]
        readme_pages = [p for p in pages if p.get("source") == "README.md" and "text" in p]
        self.assertGreaterEqual(len(readme_pages), 19)
        promoted = {r[0] for r in REAL_SPLIT}
        expected = Counter()
        for heading, lines in h1_sections(self.readme):
            if heading in ("Table of contents", self.title):
                continue
            for line in lines:
                promote = heading == "Concepts"
                expected[re.sub(r"^## (.*?)\s*$", lambda m: f"# {m.group(1)}" if promote and m.group(1) in promoted else m.group(0), line).rstrip()] += 1 if line.strip() else 0
        if "concepts" not in {p["slug"] for p in readme_pages}:
            expected["# Concepts"] -= 1
        got = Counter(line for p in readme_pages for line in lines_of(p["text"]))
        self.assertEqual(+got, +expected)

    def test_the_concepts_section_is_split_into_its_own_pages_with_the_assertions_under_reference(self):
        groups = {p["slug"]: p["group"] for p in bs.load_config(self.root)["pages"]}
        for _, slug, group in REAL_SPLIT:
            self.assertEqual(groups.get(slug), group, slug)

    def test_the_demonstration_gif_is_copied_and_the_overview_points_at_it(self):
        copies = list(self.out.rglob("venom.gif"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_bytes(), (ROOT / "venom.gif").read_bytes())
        src = re.search(r'<img [^>]*src="([^"]+)"', page_html(self.out, "overview"))
        self.assertIsNotNone(src)
        self.assertTrue((self.out / src.group(1)).is_file(), src.group(1))

    def test_no_output_file_names_a_badge_host(self):
        files = [p for p in self.out.rglob("*") if p.is_file()]
        self.assertGreater(len(files), 19)
        for path in files:
            data = path.read_bytes()
            for host in (b"shields.io", b"godoc.org", b"goreportcard.com"):
                self.assertNotIn(host, data, f"{host!r} in {path.name}")


class LocalImages(unittest.TestCase):
    GIF = b"GIF89a-synthetic-bytes"

    def build_with(self, body: str, files: dict[str, bytes] = None):
        root = split_repo(f"# Alpha\n\n{body}\n", [item("Alpha", "alpha")], skip=[])
        for rel, data in {"pic.gif": self.GIF, **(files or {})}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_bytes(data)
        out = root / "_out"
        return bs.build(root, out), out

    def assert_local_img(self, out: Path):
        src = re.search(r'<img [^>]*src="([^"]+)"', page_html(out, "alpha"))
        self.assertIsNotNone(src, page_html(out, "alpha"))
        value = src.group(1)
        self.assertFalse(re.match(r"[a-z]+:|/|\.\./", value), value)
        self.assertEqual((out / value).read_bytes(), self.GIF)

    def test_a_markdown_image_of_a_public_file_is_copied_and_emitted_as_a_local_img(self):
        errors, out = self.build_with("![Demo](./pic.gif)")
        self.assertEqual(errors, [])
        self.assert_local_img(out)

    def test_an_html_img_of_a_public_file_is_copied_and_emitted_as_a_local_img(self):
        errors, out = self.build_with('<img src="./pic.gif" alt="Demo">')
        self.assertEqual(errors, [])
        self.assert_local_img(out)
        self.assertIn('alt="Demo"', page_html(out, "alpha"))

    def test_an_image_whose_file_is_missing_is_a_build_error_naming_it(self):
        errors, _ = self.build_with("![Demo](./gone.gif)")
        self.assertTrue(any("gone.gif" in e for e in errors), errors)

    def test_an_image_in_the_private_part_is_a_build_error_and_is_not_copied(self):
        errors, out = self.build_with("![Demo](./docs/secret.gif)", {"docs/secret.gif": b"private"})
        self.assertTrue(any("docs/secret.gif" in e for e in errors), errors)
        self.assertEqual(list(out.rglob("secret.gif")), [])

    def test_a_remote_image_is_not_fetched_and_is_dropped_to_its_alt_text(self):
        errors, out = self.build_with("![Remote logo](https://example.test/logo.png)")
        self.assertEqual(errors, [])
        data = page_html(out, "alpha")
        self.assertIn("Remote logo", data)
        self.assertNotIn("<img", data)


if __name__ == "__main__":
    unittest.main()
