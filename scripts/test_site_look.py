#!/usr/bin/env python3
"""Tests of the look of the docs site: template, style, theme switch and fork notice (story #3621, task #3637).
They build a scratch repository holding a copy of the REAL `site/` assets, so a missing asset is an assertion failure.
Offline, standard library only. Run: python scripts/test_site_look.py
"""
import base64
import functools
import json
import re
import shutil
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs  # noqa: E402
from test_build_site import ROOT, write_files  # noqa: E402

BASE_URL = "https://venom.pluries.dev"
REPO_URL = "https://github.com/pluries/venom"
UPSTREAM_URL = "https://github.com/ovh/venom"
REQUIRED_ASSETS = ("template.html", "home.html", "style.css", "js/site.js", "js/theme.js", "fonts/LICENSES.md")
FORBIDDEN_WORDS = ("craticula", "ag grid", "odata", "nuget")
VOID = {"meta", "link", "br", "img", "hr", "input", "source", "area", "base", "col", "embed", "wbr", "track", "param"}
LONG_TITLE = 'Long <title> & "quoted" ' + "x" * 270


def missing_assets() -> list[str]:
    """What the real `site/` folder lacks of the files the look needs (empty when complete)."""
    site = ROOT / "site"
    missing = [f"site/{name}" for name in REQUIRED_ASSETS if not (site / name).is_file()]
    if not list((site / "fonts").glob("*.woff2")):
        missing.append("site/fonts/*.woff2")
    return missing


def scratch_site(pages: list[dict], sources: dict[str, str], extra_groups: dict[str, str] | None = None) -> tuple[Path, Path]:
    """A scratch repository: a copy of the real site assets, a site.json written here, the page sources; built into a temp folder."""
    root = Path(tempfile.mkdtemp())
    shutil.copytree(ROOT / "site", root / "site", ignore=shutil.ignore_patterns("pages", "examples", "_build", "site.json"))
    groups = {"Guides": "How-to guides.", "Concepts": "How venom works.", **(extra_groups or {})}
    config = {"title": "Pluries Venom", "repo": REPO_URL, "branch": "main", "baseUrl": BASE_URL, "groups": groups, "pages": pages}
    write_files(root, {"site/site.json": json.dumps(config), **sources})
    out = root / "_out"
    errors = bs.build(root, out)
    if errors:
        raise AssertionError(f"the scratch build failed: {errors}")
    return root, out


def page(slug: str, title: str, group: str | None = None, **extra) -> dict:
    entry = {"slug": slug, "title": title, "source": f"{slug}.md", "description": f"About {title}.", **extra}
    if group:
        entry["group"] = group
    return entry


STANDARD_PAGES = [page("index", "Home", template="site/home.html"), page("alpha", "Alpha", "Guides"), page("beta", "Beta", "Concepts")]
STANDARD_SOURCES = {"index.md": "# Home\n\nHome text.\n", "alpha.md": "# Alpha\n\nAlpha body text.\n", "beta.md": "# Beta\n\nBeta body text.\n"}


@functools.lru_cache(maxsize=None)
def _standard_cached() -> tuple[Path, Path]:
    return scratch_site(STANDARD_PAGES, STANDARD_SOURCES)


class LookCase(unittest.TestCase):
    def standard(self) -> Path:
        """The output folder of the standard scratch build; fails (not errors) when the real assets are missing."""
        missing = missing_assets()
        if missing:
            self.fail("; ".join(f"{name} does not exist" for name in missing))
        return _standard_cached()[1]

    def html_pages(self) -> dict[str, str]:
        out = self.standard()
        pages = {p.name: p.read_text(encoding="utf-8") for p in sorted(out.glob("*.html"))}
        self.assertEqual(set(pages), {"index.html", "alpha.html", "beta.html"})
        return pages


# ---------- text helpers ----------

class Anchors(HTMLParser):
    """The links inside the first <footer> and the plain text of it."""

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


def footer_of(page_html: str) -> tuple[list[str], str]:
    parser = Anchors()
    parser.feed(page_html)
    return parser.hrefs, re.sub(r"\s+", " ", "".join(parser.text))


class Footer(LookCase):
    def test_every_page_carries_the_fork_notice(self):
        for name, text in self.html_pages().items():
            _, words = footer_of(text)
            with self.subTest(page=name):
                self.assertIn("Pluries Venom is an independent fork of ovh/venom, published under the Apache-2.0 licence", words)
                self.assertIn("Apache-2.0", words)

    def test_every_page_links_upstream_the_licence_and_the_fork(self):
        for name, text in self.html_pages().items():
            hrefs, _ = footer_of(text)
            with self.subTest(page=name):
                self.assertIn(UPSTREAM_URL, hrefs)
                self.assertTrue(any(h.endswith("/blob/main/LICENSE") or h.endswith("/LICENSE") for h in hrefs), hrefs)
                self.assertIn(REPO_URL, hrefs)

    def test_no_page_outside_its_footer_names_the_project_it_was_copied_from(self):
        for name, text in self.html_pages().items():
            body = re.sub(r"<footer\b.*?</footer>", "", text, flags=re.S | re.I).lower()
            for word in FORBIDDEN_WORDS:
                with self.subTest(page=name, word=word):
                    self.assertNotIn(word, body)


# ---------- T3: placeholders ----------

class Placeholders(LookCase):
    def test_no_page_holds_an_unresolved_placeholder(self):
        for name, text in self.html_pages().items():
            with self.subTest(page=name):
                self.assertEqual(re.findall(r"\{\{\s*[\w.]+\s*\}\}", text), [])


# ---------- T4: hosts ----------

URL_TEXT = re.compile(r"(?:https?:)?//[^\s\"'<>)\\]+", re.I)
CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I | re.S)
CSS_IMPORT = re.compile(r"@import\s+(?:url\(\s*)?['\"]?([^'\")\s;]+)", re.I)


def is_remote(value: str) -> bool:
    value = value.strip()
    return bool(re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)", value)) and not value.lower().startswith("mailto:")


DATA_IMAGE = re.compile(r"^data:image/[a-z0-9.+-]+\s*(;[^,]*)?,", re.I)
SVG_PAYLOAD_FORBIDDEN = re.compile(r"<\s*script|<\s*foreignObject|xlink:href|\bhref\s*=|\bon\w+\s*=|javascript:|(?:https?:)?//", re.I)


def css_address_is_remote(value: str) -> bool:
    """A CSS url() address that is not the site's own: an inline image data URI makes no request, unless its payload holds a script or a link."""
    value = value.strip()
    if not DATA_IMAGE.match(value):
        return is_remote(value) or value.startswith("//")
    head, _, payload = value.partition(",")
    payload = base64.b64decode(payload + "=" * (-len(payload) % 4), validate=False).decode("utf-8", "replace") \
        if ";base64" in head.lower() else unquote(payload)
    payload = re.sub(r"""xmlns(?::\w+)?\s*=\s*(["'])[^"']*\1""", "", payload)
    return bool(SVG_PAYLOAD_FORBIDDEN.search(payload))


def allowed_link(value: str) -> bool:
    """A remote address a link may point to: the fork, its upstream, or the site itself."""
    return any(value == base or value.startswith(base + "/") or value.startswith(base + "#") or value.startswith(base + "?")
               for base in (REPO_URL, UPSTREAM_URL, BASE_URL))


class References(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, str, str]] = []  # (tag, attribute, value)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for attribute in ("href", "src", "action", "data", "poster", "formaction"):
            if values.get(attribute) is not None:
                self.found.append((tag, attribute, values[attribute]))
        for candidate in (values.get("srcset") or "").split(","):
            if candidate.strip():
                self.found.append((tag, "srcset", candidate.split()[0]))
        if tag == "meta" and (values.get("http-equiv") or "").lower() == "refresh":
            self.found.append((tag, "refresh", re.sub(r"^\s*\d+\s*;\s*(?:url=)?", "", values.get("content") or "", flags=re.I)))
        if tag == "iframe":
            self.found.append((tag, "iframe", values.get("src", "")))
        if values.get("style"):
            self.found.extend((tag, "style", m.group(2)) for m in CSS_URL.finditer(values["style"]))


class Hosts(LookCase):
    def output_files(self) -> dict[str, str]:
        out = self.standard()
        return {str(p.relative_to(out)): p.read_text(encoding="utf-8") for p in sorted(out.rglob("*"))
                if p.is_file() and p.suffix in (".html", ".css", ".js", ".svg")}

    def test_every_address_in_every_text_file_is_the_fork_its_upstream_or_the_site(self):
        for name, text in self.output_files().items():
            text = re.sub(r"""xmlns(?::\w+)?\s*=\s*(["'])[^"']*\1""", "", text)
            for url in URL_TEXT.findall(text):
                with self.subTest(file=name, url=url):
                    self.assertTrue(allowed_link(url.rstrip(".,;")), "address on another host")

    def test_only_links_point_to_a_remote_address_and_never_to_another_host(self):
        for name, text in self.html_pages().items():
            parser = References()
            parser.feed(text)
            for tag, attribute, value in parser.found:
                if not is_remote(value):
                    continue
                with self.subTest(page=name, tag=tag, attribute=attribute, value=value):
                    self.assertNotIn(tag, ("iframe", "form", "object", "embed"))
                    self.assertTrue(allowed_link(value), "remote address on another host")
                    if allowed_link(value) and not value.startswith(BASE_URL):
                        self.assertEqual((tag, attribute), ("a", "href"), "the GitHub addresses are links, never loaded resources")

    def test_the_stylesheet_loads_nothing_remote_and_its_fonts_are_the_sites_own_files(self):
        out = self.standard()
        css = (out / "style.css").read_text(encoding="utf-8")
        addresses = [m.group(2) for m in CSS_URL.finditer(css)] + CSS_IMPORT.findall(css)
        self.assertTrue(any(a.endswith(".woff2") for a in addresses), "the stylesheet declares self-hosted fonts")
        for address in addresses:
            with self.subTest(address=address):
                self.assertFalse(css_address_is_remote(address))
                if not address.startswith("data:"):
                    self.assertTrue((out / address.partition("?")[0].partition("#")[0]).is_file(), "no such file in the output")

    def test_the_font_files_and_their_licences_are_published(self):
        out = self.standard()
        self.assertTrue(list((out / "fonts").glob("*.woff2")))
        self.assertTrue((out / "fonts" / "LICENSES.md").is_file())


class CssAddressIsRemote(unittest.TestCase):
    SVG = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='8' height='8'%3E%3Cpath d='M0 0h8v8z'/%3E%3C/svg%3E"

    def test_an_inline_svg_image_is_not_a_request(self):
        self.assertFalse(css_address_is_remote(self.SVG))

    def test_an_inline_base64_svg_image_is_not_a_request(self):
        payload = base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'><path d='M0 0'/></svg>").decode()
        self.assertFalse(css_address_is_remote("data:image/svg+xml;base64," + payload))

    def test_a_site_file_is_not_remote(self):
        self.assertFalse(css_address_is_remote("fonts/a.woff2"))

    def test_every_network_or_other_scheme_is_remote(self):
        for address in ("https://example.org/a.png", "http://example.org/a.png", "//example.org/a.png",
                        "ftp://example.org/a.png", "javascript:alert(1)", "blob:abc", "file:///etc/x"):
            with self.subTest(address=address):
                self.assertTrue(css_address_is_remote(address))

    def test_a_data_address_of_another_type_is_remote(self):
        for address in ("data:text/html,%3Ch1%3Ex%3C/h1%3E", "data:application/javascript,alert(1)", "data:,plain"):
            with self.subTest(address=address):
                self.assertTrue(css_address_is_remote(address))

    def test_an_svg_payload_with_a_script_or_a_link_is_remote(self):
        for payload in ("%3Csvg%3E%3Cscript%3Ealert(1)%3C/script%3E%3C/svg%3E",
                        "%3Csvg%3E%3Ca href='https://example.org'/%3E%3C/svg%3E",
                        "%3Csvg%3E%3Cuse xlink:href='x.svg%23a'/%3E%3C/svg%3E",
                        "%3Csvg onload='x()'%3E%3C/svg%3E"):
            with self.subTest(payload=payload):
                self.assertTrue(css_address_is_remote("data:image/svg+xml," + payload))

    def test_a_base64_svg_payload_with_a_script_is_remote(self):
        payload = base64.b64encode(b"<svg><script>alert(1)</script></svg>").decode()
        self.assertTrue(css_address_is_remote("data:image/svg+xml;base64," + payload))


# ---------- theme switch and scripts ----------

class ThemeSwitch(LookCase):
    def test_scripts_are_referenced_by_relative_urls_and_the_theme_script_is_published(self):
        out = self.standard()
        for name, text in self.html_pages().items():
            sources = re.findall(r"<script\b[^>]*\bsrc=[\"']([^\"']+)[\"']", text)
            with self.subTest(page=name):
                self.assertIn("theme.js", sources)
                self.assertTrue(all(not is_remote(s) for s in sources), sources)
        self.assertTrue((out / "theme.js").is_file())
        self.assertTrue((out / "site.js").is_file())

    def test_the_theme_script_applies_the_stored_choice_to_data_theme(self):
        script = (self.standard() / "theme.js").read_text(encoding="utf-8")
        self.assertIn("localStorage", script)
        self.assertTrue("dataset.theme" in script or "data-theme" in script)

    def test_the_full_content_is_in_the_html_without_any_script(self):
        pages = self.html_pages()
        self.assertIn("Alpha body text.", pages["alpha.html"])
        self.assertIn("Beta body text.", pages["beta.html"])
        self.assertIn("Home text.", pages["index.html"])
        self.assertIn('<nav aria-label="Pages">', pages["alpha.html"])
        self.assertIn('class="side-group"', pages["alpha.html"])


# ---------- T6: robustness ----------

class Balance(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.problems: list[str] = []
        self.titles: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> closes {self.stack[-1] if self.stack else 'nothing'}")
            if tag in self.stack:
                while self.stack.pop() != tag:
                    pass
            return
        self.stack.pop()

    def handle_data(self, data):
        if self.in_title:
            self.titles.append(data)


class Robustness(LookCase):
    def build_stress(self) -> Path:
        missing = missing_assets()
        if missing:
            self.fail("; ".join(f"{name} does not exist" for name in missing))
        pages = [page("index", "Home", template="site/home.html"), page("long", LONG_TITLE, "Guides")]
        pages += [page(f"entry-{n:02d}", f"Entry number {n}", "Guides") for n in range(40)]
        pages += [page("beta", "Beta", "Concepts")]
        sources = {"index.md": "# Home\n\nHome text.\n", "long.md": "# Long\n\nBody.\n", "beta.md": "# Beta\n\nBody.\n",
                   **{f"entry-{n:02d}.md": f"# Entry {n}\n\nBody.\n" for n in range(40)}}
        return scratch_site(pages, sources)[1]

    def test_every_page_has_balanced_tags_with_a_long_title_many_entries_and_a_group_without_pages(self):
        out = self.build_stress()
        built = sorted(out.glob("*.html"))
        self.assertEqual(len(built), 43)
        for path in built:
            parser = Balance()
            parser.feed(path.read_text(encoding="utf-8"))
            parser.close()
            with self.subTest(page=path.name):
                self.assertEqual(parser.problems, [])
                self.assertEqual(parser.stack, [], "tags left open")

    def test_the_long_title_is_escaped_and_whole(self):
        text = (self.build_stress() / "long.html").read_text(encoding="utf-8")
        self.assertIn("Long &lt;title&gt; &amp; &quot;quoted&quot; " + "x" * 270, text.replace("&#x27;", "'"))
        self.assertNotIn("Long <title>", text)
        parser = Balance()
        parser.feed(text)
        self.assertIn(LONG_TITLE, "".join(parser.titles))

    def test_a_group_without_pages_leaves_no_sidebar_block_or_card(self):
        out = self.build_stress()
        text = (out / "long.html").read_text(encoding="utf-8")
        self.assertNotIn("<summary>Reference</summary>", text)
        self.assertNotIn("<h3>Reference</h3>", (out / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(text.count('class="side-group"'), 2)


if __name__ == "__main__":
    unittest.main()
