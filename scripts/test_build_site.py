#!/usr/bin/env python3
"""Tests of scripts/build_site.py (story #3621, task #3634): the Markdown converter, the link and anchor check, the
`{{include}}` directive, the configuration, the private-path list and the exit codes. Offline, standard library only.
Run: python scripts/test_build_site.py
"""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = "<html><title>{{page_title}}</title><nav>{{nav}}</nav>{{content}}<a href=\"{{source_url}}\">{{source}}</a></html>"
REPO = "https://example.test/r"
PUBLISH_SCRIPT = ROOT / "scripts" / "publish-github.sh"
VENOM_PRIVATE = ("legacy", "analysis", "modernized", ".claude", ".mcp.json", "CLAUDE.md", "docs", ".githooks", ".azuredevops",
                 ".sonarlint", ".vscode", "Jenkinsfile", "jenkins", "sonar-project.properties", "scripts/publish-github.sh")
INDEX = {"slug": "index", "title": "Home", "source": "index.md", "group": "Guides", "description": "The home page."}


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def make_repo(files: dict[str, str], pages: list[dict], default_group: bool = True, default_description: bool = True, **extra) -> Path:
    """A scratch repository: the site files, the pages' sources and any other file. A page without `group` gets "Guides" and
    one without `description` gets a sentence, unless the caller opts out (the refusal tests)."""
    if default_group:
        pages = [p if "group" in p else {**p, "group": "Guides"} for p in pages]
    if default_description:
        pages = [p if "description" in p else {**p, "description": "A page of the test site."} for p in pages]
    root = Path(tempfile.mkdtemp())
    config = {"title": "T", "repo": REPO, "branch": "main", "baseUrl": "https://docs.example.test", "pages": pages, **extra}
    write_files(root, {"site/site.json": json.dumps(config), "site/template.html": TEMPLATE, "site/style.css": "body{}", **files})
    return root


def run_build(files: dict[str, str], pages: list[dict], **extra):
    root = make_repo(files, pages, **extra)
    out = root / "_out"
    return bs.build(root, out), out


def resolve(href: str, files: dict[str, str] | None = None, own_anchors: set[str] | None = None, pages=None, anchors_of=None,
            source: str = "index.md", raw: bool = False):
    """Call resolve_link on a scratch repository; returns (emitted href, recorded errors)."""
    root = Path(tempfile.mkdtemp())
    write_files(root, files or {})
    errors: list[str] = []
    config = {"repo": REPO, "branch": "main"}
    pages_by_source = {p.source: p for p in (pages or [])}
    emitted = bs.resolve_link(href, source, pages_by_source, root, config, anchors_of or (lambda rel: set()), errors, 5,
                              own_anchors or set(), raw=raw)
    return emitted, errors


class Anchors(unittest.TestCase):
    def test_slugs_follow_github(self):
        self.assertEqual(bs.slugify("Why"), "why")
        self.assertEqual(bs.slugify("How it works"), "how-it-works")
        self.assertEqual(bs.slugify("`spec/` — the source of truth"), "spec--the-source-of-truth")

    def test_an_angle_bracket_in_a_heading_is_not_a_tag(self):
        self.assertEqual(bs.slugify("x <b>bold</b>"), "x-bold")

    def test_a_slugger_suffixes_duplicates_and_skips_a_taken_suffix(self):
        slugs = bs.Slugger()
        self.assertEqual([slugs("S"), slugs("S 1"), slugs("S"), slugs("S")], ["s", "s-1", "s-2", "s-3"])


class Converter(unittest.TestCase):
    def test_headings_get_stable_ids_and_the_first_h1_is_the_title(self):
        rendered = bs.render_markdown("# Hello `x`\n\ntext\n\n## Sub\n")
        self.assertIn('<h1 id="hello-x">Hello <code>x</code></h1>', rendered.html)
        self.assertIn('<h2 id="sub">Sub</h2>', rendered.html)
        self.assertEqual(rendered.title, "Hello x")
        self.assertEqual(rendered.headings, [(1, "Hello x", "hello-x"), (2, "Sub", "sub")])

    def test_duplicate_headings_get_distinct_anchors(self):
        rendered = bs.render_markdown("# A\n\n## Same\n\n## Same\n\n## Same\n")
        self.assertEqual(rendered.anchors, {"a", "same", "same-1", "same-2"})
        self.assertIn('<h2 id="same-2">Same</h2>', rendered.html)

    def test_underscores_and_a_trailing_hash_survive_in_an_anchor(self):
        rendered = bs.render_markdown("## pg_trgm index\n\n### C#\n\n## Closed ##\n")
        self.assertEqual(rendered.anchors, {"pg_trgm-index", "c", "closed"})

    def test_a_heading_inside_a_fence_is_not_a_heading(self):
        self.assertEqual(bs.render_markdown("```\n# not a heading\n```\n").anchors, set())

    def test_fenced_code_is_escaped_and_keeps_its_language(self):
        rendered = bs.render_markdown("```sql\nSELECT * FROM t WHERE a < 1 AND b > 2;\n```\n")
        self.assertIn('<pre><code class="language-sql">SELECT * FROM t WHERE a &lt; 1 AND b &gt; 2;</code></pre>', rendered.html)

    def test_tables_render_with_escaped_pipes(self):
        rendered = bs.render_markdown("| a | b |\n|---|---|\n| `x` | y \\| z |\n")
        self.assertIn("<th>a</th><th>b</th>", rendered.html)
        self.assertIn("<td><code>x</code></td><td>y | z</td>", rendered.html)

    def test_nested_and_ordered_lists_render(self):
        rendered = bs.render_markdown("- one\n  - inner\n- two\n\n1. first\n2. second\n")
        self.assertIn("<ul><li>one<ul><li>inner</li></ul></li><li>two</li></ul>", rendered.html)
        self.assertIn("<ol><li>first</li><li>second</li></ol>", rendered.html)

    def test_a_fenced_block_indented_under_a_list_item_stays_in_the_item(self):
        rendered = bs.render_markdown("- first\n\n  ```sql\n  SELECT 1;\n  ```\n- second\n")
        self.assertIn('<li>first<pre><code class="language-sql">SELECT 1;</code></pre></li><li>second</li>', rendered.html)

    def test_raw_html_is_escaped_never_passed_through(self):
        rendered = bs.render_markdown("a <script>alert(1)</script> b\n")
        self.assertNotIn("<script>", rendered.html)
        self.assertIn("&lt;script&gt;", rendered.html)

    def test_an_event_handler_attribute_never_becomes_markup(self):
        rendered = bs.render_markdown("<img src=x onerror=alert(1)>\n\n<div onclick=\"x()\">hi</div>\n")
        self.assertNotIn("<img", rendered.html)
        self.assertNotIn("<div", rendered.html)
        self.assertIn("&lt;img", rendered.html)

    def test_every_value_that_reaches_the_page_is_escaped(self):
        rendered = bs.render_markdown('[a"b](x"y.md) `<i>` **<b>**\n')
        self.assertIn('href="x&quot;y.md"', rendered.html)
        self.assertIn("<code>&lt;i&gt;</code>", rendered.html)
        self.assertNotIn("<b>", rendered.html)

    def test_links_are_recorded_with_their_line_and_can_be_rewritten(self):
        rendered = bs.render_markdown("one\n\n[x](a.md#b) and [`y`](c.md)\n", resolve=lambda href, image=False: "R:" + href)
        self.assertEqual(rendered.links, [(3, "a.md#b"), (3, "c.md")])
        self.assertIn('<a href="R:a.md#b">x</a>', rendered.html)

    def test_emphasis_and_code_do_not_interfere(self):
        rendered = bs.render_markdown("**bold** and *it* and `a*b*c` and snake_case_name\n")
        self.assertIn("<strong>bold</strong>", rendered.html)
        self.assertIn("<em>it</em>", rendered.html)
        self.assertIn("<code>a*b*c</code>", rendered.html)
        self.assertIn("snake_case_name", rendered.html)


class LinkResolution(unittest.TestCase):
    def test_a_link_to_an_existing_public_file_goes_to_github(self):
        emitted, errors = resolve("src/a.cs", {"src/a.cs": "x"})
        self.assertEqual(errors, [])
        self.assertEqual(emitted, f"{REPO}/blob/main/src/a.cs")

    def test_a_link_to_a_bare_public_directory_goes_to_the_github_tree(self):
        emitted, errors = resolve("src/", {"src/a.cs": "x"})
        self.assertEqual(errors, [])
        self.assertEqual(emitted, f"{REPO}/tree/main/src")

    def test_a_link_to_a_file_keeps_its_fragment_and_an_image_goes_to_raw(self):
        self.assertEqual(resolve("a.md#part", {"a.md": "# A\n\n## Part\n"}, anchors_of=lambda rel: {"part"})[0], f"{REPO}/blob/main/a.md#part")
        self.assertEqual(resolve("img/a.png", {"img/a.png": "x"}, raw=True)[0], f"{REPO}/raw/main/img/a.png")

    def test_a_link_to_a_page_of_the_site_becomes_a_site_link_with_its_anchor_checked(self):
        pages = [bs.Page("two", "Two", "d/two.md")]
        emitted, errors = resolve("d/two.md#part", {"d/two.md": "x"}, pages=pages, anchors_of=lambda rel: {"part"})
        self.assertEqual((emitted, errors), ("two.html#part", []))

    def test_a_link_to_a_missing_file_is_an_error_naming_the_link_and_the_line(self):
        emitted, errors = resolve("missing.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("index.md:5", errors[0])
        self.assertIn("dead link 'missing.md'", errors[0])
        self.assertEqual(emitted, "missing.md")

    def test_a_link_to_a_missing_anchor_in_the_same_page_is_an_error(self):
        _, errors = resolve("#nowhere", own_anchors={"home"})
        self.assertEqual(len(errors), 1)
        self.assertIn("dead anchor '#nowhere'", errors[0])
        self.assertEqual(resolve("#home", own_anchors={"home"})[1], [])

    def test_a_link_to_a_missing_anchor_in_another_page_or_file_is_an_error(self):
        pages = [bs.Page("two", "Two", "two.md")]
        _, errors = resolve("two.md#nowhere", {"two.md": "# Two\n"}, pages=pages, anchors_of=lambda rel: {"two"})
        self.assertEqual(len(errors), 1)
        self.assertIn("dead anchor '#nowhere' in two.md", errors[0])
        _, errors = resolve("other.md#gone", {"other.md": "# Other\n"}, anchors_of=lambda rel: {"other"})
        self.assertIn("dead anchor '#gone' in other.md", errors[0])

    def test_a_link_into_every_private_path_is_an_error_as_a_file(self):
        for entry in bs.PRIVATE_PATHS:
            with self.subTest(entry=entry):
                emitted, errors = resolve(entry, {entry: "x"})
                self.assertEqual(len(errors), 1)
                self.assertIn("private part", errors[0])
                self.assertEqual(emitted, entry)

    def test_a_link_into_every_private_path_is_an_error_as_a_bare_directory_and_below_it(self):
        for entry in bs.PRIVATE_PATHS:
            with self.subTest(entry=entry):
                files = {f"{entry}/inner.md": "x"}
                for href in (entry, entry + "/", entry + "/inner.md"):
                    errors = resolve(href, files)[1]
                    self.assertEqual(len(errors), 1, href)
                    self.assertIn("private part", errors[0])

    def test_a_private_path_is_refused_from_a_page_in_a_subfolder_too(self):
        _, errors = resolve("../docs/x.md", {"docs/x.md": "x", "sub/p.md": "x"}, source="sub/p.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("private part", errors[0])

    def test_a_private_looking_name_that_is_public_passes(self):
        for href in ("docs-site.md", "scripts/build_site.py", "documents/a.md", "jenkinsfile-notes.md"):
            with self.subTest(href=href):
                self.assertEqual(resolve(href, {href: "x"})[1], [])

    def test_a_link_that_leaves_the_repository_is_an_error(self):
        _, errors = resolve("../outside.md")
        self.assertEqual(len(errors), 1)
        self.assertIn("leaves the repository", errors[0])

    def test_a_scheme_other_than_http_https_or_mailto_is_an_error(self):
        for bad in ("ftp://example.test/x", "javascript:alert(1)", "JaVaScRiPt:alert(1)", "file:///etc/hostname", "data:text/html,x"):
            with self.subTest(bad=bad):
                emitted, errors = resolve(bad)
                self.assertEqual(len(errors), 1)
                self.assertIn("scheme other than http, https or mailto", errors[0])

    def test_http_https_and_mailto_pass_unchanged_and_are_not_fetched(self):
        for good in ("http://example.invalid/a", "https://example.invalid/never#x", "mailto:a@b.c"):
            with self.subTest(good=good):
                self.assertEqual(resolve(good), (good, []))


class Includes(unittest.TestCase):
    def expand(self, text: str, files: dict[str, str]) -> str:
        root = Path(tempfile.mkdtemp())
        write_files(root, files)
        return bs.expand_includes(text, root, "index.md")

    def test_a_public_file_is_included_as_a_code_block(self):
        out = self.expand("before\n{{include src/a.go go}}\nafter", {"src/a.go": "package a\n"})
        self.assertEqual(out, "before\n````go\npackage a\n````\nafter")

    def test_the_fence_is_longer_than_any_backtick_run_in_the_file(self):
        self.assertIn("`````text\nx\n````\ny\n`````", self.expand("{{include a.txt text}}", {"a.txt": "x\n````\ny\n"}))
        self.assertIn("````text\nx\n```\ny\n````", self.expand("{{include a.txt text}}", {"a.txt": "x\n```\ny\n"}))

    def test_a_file_with_crlf_lines_is_included_with_newlines(self):
        root = Path(tempfile.mkdtemp())
        (root / "a.txt").write_bytes(b"one\r\ntwo\r\n")
        self.assertEqual(bs.expand_includes("{{include a.txt text}}", root, "index.md"), "````text\none\ntwo\n````")

    def test_an_example_inside_a_fence_of_the_page_is_left_alone(self):
        text = "```\n{{include nothing.txt text}}\n```"
        self.assertEqual(self.expand(text, {}), text)

    def test_a_hyphenated_language_works(self):
        self.assertIn("````shell-session\nx\n````", self.expand("{{include a.txt shell-session}}", {"a.txt": "x"}))

    def test_a_private_path_is_an_error(self):
        for entry in ("docs/x.md", ".claude/rules/x.md", "CLAUDE.md", "scripts/publish-github.sh", "jenkins/a.groovy"):
            with self.subTest(entry=entry):
                with self.assertRaises(bs.BuildError):
                    self.expand(f"{{{{include {entry} text}}}}", {entry: "secret"})

    def test_a_missing_path_is_an_error(self):
        with self.assertRaises(bs.BuildError):
            self.expand("{{include src/missing.go go}}", {})

    def test_a_path_outside_the_repository_is_an_error(self):
        for bad in ("../outside.md", "a/../../outside.md", "/etc/hostname"):
            with self.subTest(bad=bad):
                with self.assertRaises(bs.BuildError):
                    self.expand(f"{{{{include {bad} text}}}}", {})

    def test_a_malformed_include_is_an_error_not_literal_text(self):
        for bad in ("{{include a.txt}}", "{{include}}", "{{include a.txt a b}}", "{{include a.txt c#}}"):
            with self.subTest(bad=bad):
                with self.assertRaises(bs.BuildError):
                    self.expand(bad, {"a.txt": "x"})

    def test_an_include_error_names_the_page_and_the_line(self):
        with self.assertRaises(bs.BuildError) as raised:
            self.expand("ok\n\n{{include nope.txt text}}", {})
        self.assertIn("index.md:3", str(raised.exception))

    def test_an_included_file_reaches_the_built_page(self):
        files = {"index.md": "# Home\n\n{{include src/a.cs csharp}}\n\nafter\n", "src/a.cs": "var x = 1 < 2;\n```\nend\n```\n"}
        errors, out = run_build(files, [INDEX])
        self.assertEqual(errors, [])
        page = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn('<pre><code class="language-csharp">var x = 1 &lt; 2;\n```\nend\n```</code></pre>', page)
        self.assertIn("<p>after</p>", page)


class Config(unittest.TestCase):
    def load(self, **changes):
        root = make_repo({"index.md": "# Home\n"}, [INDEX])
        config = json.loads((root / "site" / "site.json").read_text(encoding="utf-8"))
        for key, value in changes.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        (root / "site" / "site.json").write_text(json.dumps(config), encoding="utf-8")
        return bs.load_config(root)

    def load_pages(self, pages: list[dict], **kwargs):
        root = make_repo({"index.md": "# Home\n", "docs/x.md": "x"}, pages, **kwargs)
        return bs.load_config(root)

    def test_a_valid_configuration_loads(self):
        config = self.load()
        self.assertEqual(config["title"], "T")
        self.assertEqual(config["packages"], {})
        self.assertNotIn("ruleSource", config)
        self.assertNotIn("ruleIdPrefixes", config)

    def test_the_groups_are_the_five_venom_groups_in_order(self):
        self.assertEqual(bs.GROUPS, ("Getting started", "Concepts", "Guides", "Executors", "Reference"))

    def test_a_missing_required_key_is_an_error_naming_the_key(self):
        for key in ("title", "repo", "branch", "pages"):
            with self.subTest(key=key):
                with self.assertRaises(bs.BuildError) as raised:
                    self.load(**{key: None})
                self.assertIn(f"'{key}'", str(raised.exception))

    def test_a_missing_or_broken_site_json_is_an_error(self):
        with self.assertRaises(bs.BuildError):
            bs.load_config(Path(tempfile.mkdtemp()))
        root = make_repo({"index.md": "# Home\n"}, [INDEX])
        (root / "site" / "site.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(bs.BuildError):
            bs.load_config(root)

    def test_base_url_is_normalised(self):
        self.assertEqual(self.load(baseUrl="https://venom.example.test/docs/")["baseUrl"], "https://venom.example.test/docs")
        self.assertEqual(self.load(baseUrl="https://venom.example.test")["baseUrl"], "https://venom.example.test")

    def test_a_base_url_that_is_not_a_plain_https_address_is_an_error(self):
        for bad in ("http://venom.example.test", "ftp://venom.example.test", "https://user:pw@venom.example.test", "https://venom.example.test/?q=1",
                    "https://venom.example.test/#frag", "venom.example.test", "https://", 42):
            with self.subTest(bad=bad):
                with self.assertRaises(bs.BuildError) as raised:
                    self.load(baseUrl=bad)
                self.assertIn("baseUrl", str(raised.exception))

    def test_a_page_without_a_description_is_an_error_naming_the_page(self):
        pages = [INDEX, {"slug": "two", "title": "Two", "source": "two.md", "group": "Guides"}]
        with self.assertRaises(bs.BuildError) as raised:
            self.load_pages(pages, default_description=False)
        self.assertIn("two", str(raised.exception))
        self.assertIn("description", str(raised.exception))

    def test_a_blank_description_is_an_error(self):
        with self.assertRaises(bs.BuildError):
            self.load_pages([INDEX, {"slug": "two", "title": "Two", "source": "two.md", "description": "  "}])

    def test_a_page_with_an_unknown_missing_or_empty_group_is_an_error(self):
        for group in ("Configuration", "guides", "", 3):
            with self.subTest(group=group):
                with self.assertRaises(bs.BuildError) as raised:
                    self.load_pages([INDEX, {"slug": "two", "title": "Two", "source": "two.md", "group": group}])
                self.assertIn("two", str(raised.exception))
        with self.assertRaises(bs.BuildError):
            self.load_pages([INDEX, {"slug": "two", "title": "Two", "source": "two.md"}], default_group=False)

    def test_every_one_of_the_five_groups_is_accepted_for_a_page(self):
        pages = [INDEX] + [{"slug": f"p{n}", "title": name, "source": f"p{n}.md", "group": name} for n, name in enumerate(bs.GROUPS)]
        self.assertEqual(len(self.load_pages(pages)["pages"]), 6)

    def test_the_index_page_may_have_no_group(self):
        config = self.load_pages([{"slug": "index", "title": "Home", "source": "index.md", "group": None, "description": "Home."}])
        self.assertEqual(len(config["pages"]), 1)

    def test_the_groups_key_accepts_only_the_five_groups(self):
        self.assertEqual(set(self.load(groups={name: "A sentence." for name in bs.GROUPS})["groups"]), set(bs.GROUPS))
        for bad in ({"Configuration": "A sentence."}, {"Guides": 3}, ["Guides"]):
            with self.subTest(bad=bad):
                with self.assertRaises(bs.BuildError):
                    self.load(groups=bad)

    def test_page_slugs_must_be_safe_unique_and_include_index(self):
        for pages in ([INDEX, {"slug": "../x", "title": "X", "source": "x.md"}], [INDEX, {"slug": 'a"b', "title": "X", "source": "x.md"}],
                      [INDEX, {"slug": "index", "title": "Again", "source": "x.md"}],
                      [{"slug": "a", "title": "A", "source": "a.md"}]):
            with self.subTest(pages=pages):
                with self.assertRaises(bs.BuildError):
                    self.load_pages(pages)

    def test_a_page_source_must_be_a_public_file_inside_the_repository(self):
        for source in ("/etc/hostname", "../outside.md", "docs/x.md", "CLAUDE.md"):
            with self.subTest(source=source):
                with self.assertRaises(bs.BuildError):
                    self.load_pages([INDEX, {"slug": "x", "title": "X", "source": source}])
        with self.assertRaises(bs.BuildError):
            self.load_pages([INDEX, {"slug": "x", "title": "X"}])

    def test_packages_must_map_a_name_to_an_https_address(self):
        self.assertEqual(self.load(packages={"go": "https://pkg.go.dev/x"})["packages"], {"go": "https://pkg.go.dev/x"})
        with self.assertRaises(bs.BuildError):
            self.load(packages={"go": "http://pkg.go.dev/x"})


class PrivateList(unittest.TestCase):
    def test_the_private_list_is_the_venom_one_written_out(self):
        self.assertEqual(tuple(bs.PRIVATE_PATHS), VENOM_PRIVATE)

    def test_private_means_the_entry_and_everything_below_it_only(self):
        self.assertTrue(bs._is_private("docs"))
        self.assertTrue(bs._is_private("docs/"))
        self.assertTrue(bs._is_private("docs/spec/public-launch.md"))
        self.assertTrue(bs._is_private("scripts/publish-github.sh"))
        self.assertFalse(bs._is_private("docs-site.md"))
        self.assertFalse(bs._is_private("scripts/build_site.py"))
        self.assertFalse(bs._is_private("site/pages/index.md"))
        self.assertFalse(bs._is_private("executors/http/README.md"))

    @unittest.skipUnless(PUBLISH_SCRIPT.exists(), "scripts/publish-github.sh is private (absent from the public mirror)")
    def test_the_private_list_equals_the_private_array_of_the_publish_script(self):
        match = re.search(r"(?ms)^PRIVATE=\((.*?)\)", PUBLISH_SCRIPT.read_text(encoding="utf-8"))
        self.assertIsNotNone(match, "no PRIVATE array in scripts/publish-github.sh")
        self.assertEqual({e.rstrip("/") for e in match.group(1).split()}, set(bs.PRIVATE_PATHS),
                         "PRIVATE_PATHS differs from the PRIVATE array of scripts/publish-github.sh")


class Cli(unittest.TestCase):
    def run_main(self, root: Path):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = bs.main(["--root", str(root), "--out", str(root / "_out")])
        return code, err.getvalue()

    def test_a_clean_site_builds_end_to_end_and_exits_0(self):
        pages = [INDEX, {"slug": "two", "title": "Two", "source": "d/two.md", "group": "Executors"}]
        files = {"index.md": "# Home\n\n[go](d/two.md#part) [code](src/a.go) [out](https://example.invalid/x)\n",
                 "d/two.md": "# Two\n\n## Part\n\n{{include src/a.go go}}\n", "src/a.go": "package a\n"}
        root = make_repo(files, pages)
        code, err = self.run_main(root)
        self.assertEqual((code, err), (0, ""))
        out = root / "_out"
        home = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn('href="two.html#part"', home)
        self.assertIn(f'href="{REPO}/blob/main/src/a.go"', home)
        self.assertIn("package a", (out / "two.html").read_text(encoding="utf-8"))
        self.assertTrue((out / "style.css").exists())

    def test_exit_1_on_a_dead_link_naming_it(self):
        code, err = self.run_main(make_repo({"index.md": "# Home\n\n[x](nope.md)\n"}, [INDEX]))
        self.assertEqual(code, 1)
        self.assertIn("nope.md", err)

    def test_exit_1_on_a_private_link_even_when_the_file_exists(self):
        code, err = self.run_main(make_repo({"index.md": "# Home\n\n[x](docs/a.md)\n", "docs/a.md": "x"}, [INDEX]))
        self.assertEqual(code, 1)
        self.assertIn("private part", err)

    def test_the_build_function_returns_the_errors_of_a_dead_link(self):
        errors, _ = run_build({"index.md": "# Home\n\nok\n\n[x](missing.md)\n"}, [INDEX])
        self.assertEqual(len(errors), 1)
        self.assertIn("index.md:5", errors[0])

    def test_exit_2_on_a_missing_or_invalid_configuration(self):
        self.assertEqual(self.run_main(Path(tempfile.mkdtemp()))[0], 2)
        broken = make_repo({"index.md": "# Home\n"}, [INDEX])
        (broken / "site" / "site.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.run_main(broken)[0], 2)

    def test_exit_2_on_a_missing_required_key_naming_it(self):
        root = make_repo({"index.md": "# Home\n"}, [INDEX])
        config = json.loads((root / "site" / "site.json").read_text(encoding="utf-8"))
        del config["branch"]
        (root / "site" / "site.json").write_text(json.dumps(config), encoding="utf-8")
        code, err = self.run_main(root)
        self.assertEqual(code, 2)
        self.assertIn("branch", err)

    def test_exit_2_on_a_private_include(self):
        code, _ = self.run_main(make_repo({"index.md": "# Home\n\n{{include docs/x.md text}}\n", "docs/x.md": "x"}, [INDEX]))
        self.assertEqual(code, 2)


MARKER = "PRIVATE-MARKER-xyz"
# One spelling per way the file system reaches a private path under another name (case, trailing dot, NTFS stream).
PRIVATE_SPELLINGS = ("DOCS/secret.md", "claude.md", "docs./secret.md", "CLAUDE.md::$DATA")


def make_link(link: Path, target: Path) -> bool:
    """A directory symlink, or on Windows a junction when the symlink needs a privilege; False when neither can be made."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        if sys.platform != "win32":
            return False
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, check=False)
    return done.returncode == 0 and link.exists()


def remove_link(link: Path) -> None:
    """Removes what make_link made: a symlink with unlink, a Windows junction with rmdir."""
    if os.path.islink(link):
        os.unlink(link)
    else:
        os.rmdir(link)


def is_case_insensitive() -> bool:
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "probe.txt").write_text("x", encoding="utf-8")
        return (Path(folder) / "PROBE.TXT").exists()


class PrivateSpellingCase(unittest.TestCase):
    """Base of T7 to T10: a scratch repository with a private docs/secret.md and CLAUDE.md that both hold MARKER."""

    def repo(self, files: dict[str, str], pages=(INDEX,), link: bool = False, **extra) -> Path:
        private = {"docs/secret.md": f"# Secret\n\n{MARKER}\n", "CLAUDE.md": f"# Private\n\n{MARKER}\n"}
        root = make_repo({**private, **files}, list(pages), **extra)
        self.addCleanup(shutil.rmtree, root, True)
        if link:
            pub = root / "pub"
            if not make_link(pub, root / "docs"):
                self.skipTest("no symlink or junction can be created here (no privilege)")
            self.addCleanup(remove_link, pub)  # cleanups run last-in first-out: the link goes before the tree
        return root

    @staticmethod
    def main(root: Path):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = bs.main(["--root", str(root), "--out", str(root / "_out")])
        return code, err.getvalue()

    def assert_no_marker(self, root: Path, err: str) -> None:
        self.assertNotIn(MARKER, err)
        for produced in (root / "_out").rglob("*"):
            if produced.is_file():
                self.assertNotIn(MARKER.encode(), produced.read_bytes(), f"private text in {produced.name}")


class T7IncludeSpellings(PrivateSpellingCase):
    def include_build(self, spelling: str, link: bool = False):
        root = self.repo({"index.md": f"# Home\n\n{{{{include {spelling} text}}}}\n"}, link=link)
        code, err = self.main(root)
        return root, code, err

    def test_t7a_an_include_of_a_private_file_in_another_spelling_is_a_configuration_error(self):
        for spelling in PRIVATE_SPELLINGS:
            with self.subTest(spelling=spelling):
                root, code, err = self.include_build(spelling)
                self.assertEqual(code, 2, err)
                self.assertTrue("private" in err or "public file" in err, err)
                self.assert_no_marker(root, err)

    def test_t7a_an_include_through_a_link_that_points_at_docs_is_a_configuration_error(self):
        root, code, err = self.include_build("pub/secret.md", link=True)
        self.assertEqual(code, 2, err)
        self.assertTrue("private" in err or "public file" in err, err)
        self.assert_no_marker(root, err)

    def test_t7b_a_link_to_a_private_file_in_another_spelling_is_a_build_error_naming_the_private_part(self):
        for spelling in PRIVATE_SPELLINGS[:3]:
            with self.subTest(spelling=spelling):
                root = self.repo({"index.md": f"# Home\n\n[x]({spelling})\n"})
                code, err = self.main(root)
                self.assertEqual(code, 1, err)
                self.assertIn("private part", err)
                self.assert_no_marker(root, err)

    def test_t7b_a_link_through_a_link_that_points_at_docs_is_a_build_error_naming_the_private_part(self):
        root = self.repo({"index.md": "# Home\n\n[x](pub/secret.md)\n"}, link=True)
        code, err = self.main(root)
        self.assertEqual(code, 1, err)
        self.assertIn("private part", err)
        self.assert_no_marker(root, err)


class T8ConfigurationSpellings(PrivateSpellingCase):
    def build_with(self, key: str, value: str, link: bool = False):
        root = self.repo({"index.md": "# Home\n"}, pages=[{**INDEX, key: value}], link=link)
        code, err = self.main(root)
        return root, code, err

    def test_t8_a_page_source_in_another_spelling_is_a_configuration_error_naming_the_key(self):
        for spelling in (*PRIVATE_SPELLINGS, "../outside.md"):
            with self.subTest(spelling=spelling):
                root, code, err = self.build_with("source", spelling)
                self.assertEqual(code, 2, err)
                self.assertIn("source", err)
                self.assert_no_marker(root, err)

    def test_t8_a_page_template_in_another_spelling_is_a_configuration_error_naming_the_key(self):
        for spelling in (*PRIVATE_SPELLINGS, "../outside.html"):
            with self.subTest(spelling=spelling):
                root, code, err = self.build_with("template", spelling)
                self.assertEqual(code, 2, err)
                self.assertIn("template", err)
                self.assert_no_marker(root, err)

    def test_t8_a_source_or_template_through_a_link_that_points_at_docs_is_a_configuration_error(self):
        for key in ("source", "template"):
            with self.subTest(key=key):
                root, code, err = self.build_with(key, "pub/secret.md", link=True)
                self.assertEqual(code, 2, err)
                self.assertIn(key, err)
                self.assert_no_marker(root, err)

    def test_t8_a_public_page_spelled_in_another_case_still_builds(self):
        if not is_case_insensitive():
            self.skipTest("a case-sensitive file system: Pages/Index.md is a different file")
        root = self.repo({"index.md": "# Home\n", "pages/index.md": "# Public page\n"}, pages=[{**INDEX, "source": "Pages/Index.md"}])
        code, err = self.main(root)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Public page", (root / "_out" / "index.html").read_text(encoding="utf-8"))


class T9LinkTargets(PrivateSpellingCase):
    TICK = chr(96)
    HOSTILE = f'[a](http:{TICK}"onmouseover="alert(1){TICK})'

    def test_t9_a_link_target_that_holds_a_code_span_is_a_build_error_naming_the_link(self):
        root = self.repo({"index.md": f"# Home\n\n{self.HOSTILE}\n"})
        code, err = self.main(root)
        self.assertEqual(code, 1, err)
        self.assertIn("index.md:3", err)

    def test_t9_no_href_value_holds_an_unescaped_double_quote_or_markup(self):
        page = bs.render_markdown(f"# T\n\n{self.HOSTILE}\n\n| h |\n|---|\n| {self.HOSTILE} |\n").html

        class Attributes(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self.names: list[str] = []

            def handle_starttag(self, tag, attrs):
                self.names.extend(name for name, _ in attrs)

        parser = Attributes()
        parser.feed(page)
        self.assertEqual([n for n in parser.names if n.startswith("on")], [], page)
        for match in re.finditer(r'href="([^"]*)"', page):
            self.assertNotIn("<", match.group(1), page)
            self.assertIn(page[match.end():match.end() + 1], (">", " "), page)


class T10MutationGaps(PrivateSpellingCase):
    def test_t10a_a_private_or_outside_template_is_refused_naming_the_key(self):
        # Mutation guard: dropping the template check of site.json turns this red (characterization, green today).
        for template in ("docs/secret.md", "CLAUDE.md", "../outside.html", "/etc/passwd"):
            with self.subTest(template=template):
                root = self.repo({"index.md": "# Home\n"}, pages=[{**INDEX, "template": template}])
                code, err = self.main(root)
                self.assertEqual(code, 2, err)
                self.assertIn("template", err)
                self.assert_no_marker(root, err)

    def test_t10b_an_uppercase_allowed_scheme_passes_unchanged(self):
        # Mutation guard: dropping the .lower() of the scheme check turns this red (characterization, green today).
        for href in ("HTTP://example.test/a", "Https://example.test/a", "MAILTO:a@example.test"):
            with self.subTest(href=href):
                self.assertEqual(resolve(href), (href, []))

    def test_t10b_an_uppercase_scheme_that_is_not_allowed_is_still_an_error(self):
        _, errors = resolve("FTP://example.test/a")
        self.assertEqual(len(errors), 1)
        self.assertIn("scheme", errors[0])

    def test_t10c_an_absolute_link_outside_the_repository_is_refused_as_leaving_it(self):
        root = self.repo({"index.md": "# Home\n\n[x](/Windows/win.ini)\n"})
        code, err = self.main(root)
        self.assertEqual(code, 1, err)
        self.assertIn("leaves the repository", err)

    def test_t10c_a_network_path_is_refused_as_leaving_the_repository(self):
        # The assertion is on the error only; the host name cannot resolve, so a stat of the path fails fast.
        for href in ("//host.invalid/share/x", "/Windows/win.ini"):
            with self.subTest(href=href):
                emitted, errors = resolve(href)
                self.assertEqual(len(errors), 1, emitted)
                self.assertIn("leaves the repository", errors[0])


class T11SharedAssets(PrivateSpellingCase):
    """Files copied from site/ (stylesheet, scripts, fonts) must not be a way to copy a private file into the output."""

    SHARED = {"site/js/theme.js": "// theme\n", "site/js/site.js": "// site\n", "site/fonts/x.woff2": "font-bytes"}
    PRIVATE_COPIES = {"docs/theme.js": f"// {MARKER}\n", "docs/site.js": f"// {MARKER}\n", "docs/x.woff2": f"font {MARKER}"}

    def folder_link_repo(self, folder: str) -> Path:
        """site/<folder> becomes a link to docs/, which holds a file of the same name as the real asset."""
        root = self.repo({"index.md": "# Home\n", **self.PRIVATE_COPIES, **{k: v for k, v in self.SHARED.items() if not k.startswith(folder)}})
        link = root / folder
        if not make_link(link, root / "docs"):
            self.skipTest("no symlink or junction can be created here (no privilege)")
        self.addCleanup(remove_link, link)  # runs before the tree is removed
        return root

    def assert_refused(self, root: Path) -> None:
        code, err = self.main(root)
        self.assertEqual(code, 2, err)
        self.assertIn("must be a public file inside the repository", err)
        self.assert_no_marker(root, err)

    def test_t11_a_script_folder_that_links_into_docs_is_refused(self):
        self.assert_refused(self.folder_link_repo("site/js"))

    def test_t11_a_font_folder_that_links_into_docs_is_refused(self):
        self.assert_refused(self.folder_link_repo("site/fonts"))

    def test_t11_a_stylesheet_that_is_a_symlink_to_a_private_file_is_refused(self):
        for target in ("docs/secret.md", "CLAUDE.md"):
            with self.subTest(target=target):
                root = self.repo({"index.md": "# Home\n", **self.SHARED})
                (root / "site" / "style.css").unlink()
                try:
                    os.symlink(root / target, root / "site" / "style.css")
                except (OSError, NotImplementedError):
                    self.skipTest("no file symlink can be created here (no privilege)")
                self.assert_refused(root)

    def test_t11_a_script_or_font_that_is_a_symlink_to_a_private_file_is_refused(self):
        for asset in self.SHARED:
            with self.subTest(asset=asset):
                root = self.repo({"index.md": "# Home\n", **self.SHARED})
                (root / asset).unlink()
                try:
                    os.symlink(root / "docs" / "secret.md", root / asset)
                except (OSError, NotImplementedError):
                    self.skipTest("no file symlink can be created here (no privilege)")
                self.assert_refused(root)

    def test_t11_public_files_at_those_paths_are_still_copied(self):
        root = self.repo({"index.md": "# Home\n", **self.SHARED, "site/style.css": "body{color:red}"})
        code, err = self.main(root)
        self.assertEqual((code, err), (0, ""))
        out = root / "_out"
        self.assertEqual((out / "style.css").read_text(encoding="utf-8"), "body{color:red}")
        self.assertEqual((out / "theme.js").read_text(encoding="utf-8"), "// theme\n")
        self.assertEqual((out / "fonts" / "x.woff2").read_bytes(), b"font-bytes")


if __name__ == "__main__":
    unittest.main()
