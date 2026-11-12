#!/usr/bin/env python3
"""Build the venom docs site from the repository's own Markdown (story #3621, task #3634).

Standard library only, so the build needs no new dependency. The pages are
listed in `site/site.json`; each page is a Markdown file of the repository (a page written for the site, or a public document such
as a README that the site renders as it is). The output is a folder of static HTML files.

The build also checks every link of every page, and fails (exit 1) on:
  * a link to a file that does not exist in the repository;
  * a link to a heading anchor that does not exist (in a page of the site, or in another Markdown file of the repository);
  * a link into the private part of the repository (the PRIVATE array of `scripts/publish-github.sh`, which the public mirror
    does not carry, written out in PRIVATE_PATHS), as a file or as a bare directory;
  * a link that leaves the repository, or uses a scheme other than http, https or mailto;
  * the same for an image (`![alt](path)`), whose relative path is also rewritten to its public address.
A link to an existing repository file that has no page on the site is rewritten to its public GitHub address.
External `http(s)` links are not fetched (the build must work offline).

A page may include a repository file as a code block with a line `{{include <path> <language>}}`: the file is the single source of
the code the page shows, so a quickstart cannot drift from its page. The path must be a public file inside the repository.

Raw HTML in a source is escaped, never passed through.

    python3 scripts/build_site.py [--out DIR] [--root DIR] [--base-url URL]

Exit codes: 0 built and every link resolves, 1 a link is dead or private, 2 bad usage or an unreadable configuration.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Callable

import site_checks
import site_executors
import site_readme_split

# The PRIVATE array of scripts/publish-github.sh: what the public mirror does not carry, so a link to one is dead there.
# test_build_site.py pins this tuple to the real array where that script exists (the private repository).
PRIVATE_PATHS = ("legacy", "analysis", "modernized", ".claude", ".mcp.json", "CLAUDE.md", "docs", ".githooks", ".azuredevops",
                 ".sonarlint", ".vscode", "Jenkinsfile", "jenkins", "sonar-project.properties", "scripts/publish-github.sh")
GROUPS = ("Getting started", "Concepts", "Guides", "Executors", "Reference")  # the sidebar's fixed order

EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:)")
ALLOWED_SCHEMES = ("http:", "https:", "mailto:")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class BuildError(Exception):
    """A configuration problem (exit 2)."""


def slugify(text: str) -> str:
    """GitHub's heading slug: lower case, drop punctuation, spaces to hyphens (so existing `#...` links keep working)."""
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)
    return text.replace(" ", "-")


class Slugger:
    """GitHub's de-duplication: the first `same` is `same`, then `same-1`, `same-2`; a suffix that is already taken (by a
    heading literally called `same 1`) is skipped, so every id on the page is unique."""

    def __init__(self) -> None:
        self.used: set[str] = set()
        self.counts: dict[str, int] = {}

    def __call__(self, text: str) -> str:
        base = slugify(text)
        candidate = base
        while candidate in self.used:
            n = self.counts.get(base, 0) + 1
            self.counts[base] = n
            candidate = f"{base}-{n}"
        self.used.add(candidate)
        return candidate


@dataclass
class Rendered:
    html: str
    anchors: set[str] = field(default_factory=set)
    links: list[tuple[int, str]] = field(default_factory=list)  # (source line, href)
    title: str = ""
    headings: list[tuple[int, str, str]] = field(default_factory=list)  # level, text and anchor of each heading


_INLINE_CODE = re.compile(r"(`+)(.+?)\1", re.S)
_LINK = re.compile(r"(!?)\[((?:[^\[\]]|\[[^\]]*\])*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])")
_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>")
_REMOTE_IMAGE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]+:|[/\\]{2}")
_TAGS = re.compile(r"<[^>]+>")
_RAW_IMG = re.compile(r"<img((?:\s+[A-Za-z]+\s*=\s*(?:\"[^\"<>\x00]*\"|'[^'<>\x00]*'))+)\s*/?>", re.I)
_IMG_ATTRIBUTE = re.compile(r"\s+([A-Za-z]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_IMG_ATTRIBUTES = ("src", "alt", "width", "height", "title")
_IMG_SIZE = re.compile(r"^\d{1,4}$")


def _img_attributes(raw: str) -> dict[str, str] | None:
    """The attributes of a raw `<img>` tag when it is nothing but src, alt, width, height and title (each once, in plain quotes,
    a size in digits, a src that is not empty); None for anything else, which then stays escaped text."""
    found: dict[str, str] = {}
    for name, double, single in _IMG_ATTRIBUTE.findall(raw):
        name = name.lower()
        value = html.unescape(double or single)
        if name not in _IMG_ATTRIBUTES or name in found or "\x00" in value:
            return None
        found[name] = value
    sizes_ok = all(_IMG_SIZE.match(found[name]) for name in ("width", "height") if name in found)
    return found if sizes_ok and found.get("src", "").strip() else None


class _InlineRenderer:
    """Inline Markdown to HTML. Code spans and links are held as NUL-delimited placeholders while the rest is escaped, then restored.
    `resolve(href, image)` returns the href to emit; every link is recorded for the checker."""

    def __init__(self, out: Rendered, resolve) -> None:
        self.out, self.resolve = out, resolve
        self.holes: list[str] = []

    def render(self, text: str, line: int) -> str:
        return self._restore(self._convert(text, line))

    def _hold(self, fragment: str) -> str:
        self.holes.append(fragment)
        return f"\x00{len(self.holes) - 1}\x00"

    def _restore(self, value: str) -> str:
        while "\x00" in value:  # a held link may itself hold a code span
            value = re.sub(r"\x00(\d+)\x00", lambda m: self.holes[int(m.group(1))], value)
        return value

    def _convert(self, text: str, line: int) -> str:
        text = _INLINE_CODE.sub(lambda m: self._hold(f"<code>{html.escape(m.group(2).strip(), quote=False)}</code>"), text)
        text = _RAW_IMG.sub(lambda m: self._raw_image(m, line), text)
        text = _AUTOLINK.sub(lambda m: self._hold(f'<a href="{html.escape(m.group(1))}">{html.escape(m.group(1), quote=False)}</a>'), text)
        text = _LINK.sub(lambda m: self._link(m, line), text)
        text = html.escape(text, quote=False)
        text = _BOLD.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
        return _ITALIC.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)

    def _link(self, match: re.Match, line: int) -> str:
        bang, label, href = match.group(1), match.group(2), match.group(3)
        label_html = self._convert(label, line)
        self.out.links.append((line, href))
        target = self.resolve(href, bang == "!")
        if "\x00" in href:  # the checker has recorded the error; a held code span must never reach an attribute
            target = "#"
        if not bang:
            return self._hold(f'<a href="{html.escape(target)}">{label_html}</a>')
        return self._hold(self._image(target, _TAGS.sub("", self._restore(label_html))))

    def _raw_image(self, match: re.Match, line: int) -> str:
        """A self-contained `<img>` is rebuilt from its validated attributes, every value escaped; the original text is never
        emitted. Handled like a Markdown image: a remote one is its alt text, a local one goes through the resolver."""
        attributes = _img_attributes(match.group(1))
        if attributes is None:
            return match.group(0)
        src = attributes["src"].strip()
        self.out.links.append((line, src))
        target = self.resolve(src, True)
        alt = attributes.get("alt", "")
        if _REMOTE_IMAGE.match(target):
            return self._hold(f'<span class="img-alt">{html.escape(alt.strip(), quote=False)}</span>' if alt.strip() else "")
        extras = "".join(f' {name}="{html.escape(attributes[name])}"' for name in ("width", "height", "title") if name in attributes)
        return self._hold(f'<img alt="{html.escape(alt)}" src="{html.escape(target)}"{extras}>')

    @staticmethod
    def _image(href: str, alt: str) -> str:
        # A browser loads nothing from another host: a remote image (any scheme or spelling: HTTP:, https:/h, \\h) is its alt
        # text. A one-letter "scheme" is a drive letter, not a fetch.
        if _REMOTE_IMAGE.match(href):
            alt = alt.strip()
            return f'<span class="img-alt">{alt}</span>' if alt else ""
        return f'<img alt="{html.escape(alt)}" src="{html.escape(href)}">'


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
_FENCE = re.compile(r"^(\s*)(```+|~~~+)\s*([\w+-]*)")
_FENCE_LINE = re.compile(r"^\s*(`{3,}|~{3,})")
_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


def _cells(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", row)]


class _Blocks:
    """Block-level Markdown to HTML, one method per kind of block; each returns True when it consumed the current line."""

    def __init__(self, source: str, resolve) -> None:
        self.resolve = resolve
        self.out = Rendered(html="")
        self.slugs = Slugger()
        self.body: list[str] = []
        self.lines = source.replace("\x00", "�").replace("\r\n", "\n").split("\n")  # NUL marks the inline placeholders
        self.i = 0
        self.handlers = (self._fence, self._heading, self._rule, self._quote, self._table, self._list)

    def inline(self, text: str, number: int) -> str:
        return _InlineRenderer(self.out, self.resolve).render(text, number)

    def run(self) -> Rendered:
        while self.i < len(self.lines):
            self._step()
        self.out.html = "\n".join(self.body)
        return self.out

    def _step(self) -> None:
        line = self.lines[self.i]
        if not line.strip():
            self.i += 1
        elif not any(handler(line) for handler in self.handlers):
            self._paragraph(line)

    def _fence_html(self, opener: re.Match) -> str:
        marker, language, indent = opener.group(2), opener.group(3), len(opener.group(1))
        closing = re.compile(rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$")
        self.i += 1
        code: list[str] = []
        while self.i < len(self.lines) and not closing.match(self.lines[self.i]):
            current = self.lines[self.i]
            code.append(current[indent:] if current[:indent].strip() == "" else current)
            self.i += 1
        self.i += 1
        cls = f' class="language-{html.escape(language)}"' if language else ""
        return f"<pre><code{cls}>{html.escape(chr(10).join(code), quote=False)}</code></pre>"

    def _fence(self, line: str) -> bool:
        opener = _FENCE.match(line)
        if not opener:
            return False
        self.body.append(self._fence_html(opener))
        return True

    def _heading(self, line: str) -> bool:
        match = _HEADING.match(line)
        if not match:
            return False
        level, text = len(match.group(1)), match.group(2)
        plain = re.sub(r"[`*]", "", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text))
        anchor = self.slugs(plain)
        self.out.anchors.add(anchor)
        self.out.headings.append((level, plain, anchor))
        if level == 1 and not self.out.title:
            self.out.title = re.sub(r"[`*]", "", text)
        self.body.append(f'<h{level} id="{anchor}">{self.inline(text, self.i + 1)}</h{level}>')
        self.i += 1
        return True

    def _rule(self, line: str) -> bool:
        if not _RULE.match(line):
            return False
        self.body.append("<hr>")
        self.i += 1
        return True

    def _quote(self, line: str) -> bool:
        if not line.lstrip().startswith(">"):
            return False
        start = self.i
        quote: list[str] = []
        while self.i < len(self.lines) and self.lines[self.i].lstrip().startswith(">"):
            quote.append(re.sub(r"^\s*>\s?", "", self.lines[self.i]))
            self.i += 1
        inner = render_markdown("\n".join(quote), self.resolve)
        self.out.links.extend((start + n, href) for n, href in inner.links)
        self.out.anchors.update(inner.anchors)
        self.body.append(f"<blockquote>{inner.html}</blockquote>")
        return True

    def _table_at(self, index: int) -> bool:
        lines = self.lines
        return "|" in lines[index] and index + 1 < len(lines) and bool(_TABLE_SEP.match(lines[index + 1])) and "|" in lines[index + 1]

    def _table(self, line: str) -> bool:
        if not self._table_at(self.i):
            return False
        number = self.i + 1
        head = _cells(line)
        self.i += 2
        parts = ["<table><thead><tr>" + "".join(f"<th>{self.inline(c, number)}</th>" for c in head) + "</tr></thead><tbody>"]
        while self.i < len(self.lines) and self.lines[self.i].strip() and "|" in self.lines[self.i]:
            cells = _cells(self.lines[self.i])
            parts.append("<tr>" + "".join(f"<td>{self.inline(c, self.i + 1)}</td>" for c in cells) + "</tr>")
            self.i += 1
        parts.append("</tbody></table>")
        self.body.append("".join(parts))
        return True

    def _list(self, line: str) -> bool:
        if not _LIST.match(line):
            return False
        # (line, indent, ordered, parts): a part is a list of text lines or an HTML string for a fenced block, in source order
        items: list[tuple[int, int, bool, list]] = []
        while self.i < len(self.lines) and self._list_line(items):
            pass
        self.body.append(_render_list(items, self.inline))
        return True

    def _list_line(self, items: list) -> bool:
        line = self.lines[self.i]
        match = _LIST.match(line)
        if match:
            items.append((self.i + 1, len(match.group(1).expandtabs(4)), match.group(2)[0].isdigit(), [[match.group(3)]]))
            self.i += 1
            return True
        if not line.strip():
            return self._blank_in_list(items)
        if not items or not line.startswith((" ", "\t")):
            return False
        opener = _FENCE.match(line)
        if opener:
            items[-1][3].append(self._fence_html(opener))
        else:
            self._continue_item(items[-1][3], line)
        return True

    def _blank_in_list(self, items: list) -> bool:
        """A blank line stays inside the list only when an indented list item or fenced block follows it."""
        j = self.i
        while j < len(self.lines) and not self.lines[j].strip():
            j += 1
        if not items or j >= len(self.lines) or not self.lines[j].startswith(("  ", "\t")):
            return False
        if not (_FENCE.match(self.lines[j]) or _LIST.match(self.lines[j])):
            return False
        self.i = j
        return True

    def _continue_item(self, parts: list, line: str) -> None:
        if isinstance(parts[-1], str):  # text after a fenced block is a new paragraph of the item
            parts.append([])
        parts[-1].append(line.strip())
        self.i += 1

    def _starts_block(self, index: int) -> bool:
        line = self.lines[index]
        return bool(_HEADING.match(line) or _FENCE.match(line) or _LIST.match(line) or line.lstrip().startswith(">")
                    or _RULE.match(line) or self._table_at(index))

    def _paragraph(self, line: str) -> None:
        start = self.i + 1
        parts = [line.strip()]
        self.i += 1
        while self.i < len(self.lines) and self.lines[self.i].strip() and not self._starts_block(self.i):
            parts.append(self.lines[self.i].strip())
            self.i += 1
        self.body.append(f"<p>{self.inline(' '.join(parts), start)}</p>")


def render_markdown(source: str, resolve=lambda href, image=False: href) -> Rendered:
    """Markdown to HTML for the subset the repository's documents use (headings, paragraphs, lists, tables, fenced code,
    block quotes, rules, inline code, links, emphasis). Raw HTML in the source is escaped, never passed through."""
    return _Blocks(source, resolve).run()


def _render_list(items, inline) -> str:
    """Nested lists by indentation: an item indented deeper than the previous one opens a child list; a fenced block indented
    under an item stays inside that item."""
    html_parts: list[str] = []
    stack: list[tuple[int, str]] = []  # (indent, tag)
    for number, indent, ordered, parts in items:
        tag = "ol" if ordered else "ul"
        while stack and indent < stack[-1][0]:
            html_parts.append(f"</li></{stack.pop()[1]}>")
        if not stack or indent > stack[-1][0]:
            html_parts.append(f"<{tag}>")
            stack.append((indent, tag))
        else:
            html_parts.append("</li>")
        html_parts.append(f"<li>{_item_content(parts, number, inline)}")
    while stack:
        html_parts.append(f"</li></{stack.pop()[1]}>")
    return "".join(html_parts)


def _item_content(parts: list, number: int, inline) -> str:
    content = []
    for position, part in enumerate(parts):
        if isinstance(part, str):
            content.append(part)
        else:
            text = inline(" ".join(part), number)
            content.append(text if position == 0 else f"<p>{text}</p>")
    return "".join(content)


@dataclass
class Page:
    slug: str
    title: str
    source: str  # repository-relative file the page reads
    rendered: Rendered | None = None
    group: str | None = None
    template: str | None = None
    description: str = ""
    subgroup: str | None = None  # one nesting level under the group
    text: str | None = None  # a page cut out of a larger file carries its own Markdown instead of reading `source`
    heading_index: list[int] | None = None  # with `text`: the README-wide ordinal of each of the page's headings


def _layout(pages: list["Page"]) -> dict[str, list]:
    """Group -> its entries in order, where an entry is a page or a (subgroup name, pages) pair placed where the subgroup's first
    page appears. A page without a group (the home page) is not part of the sidebar."""
    layout: dict[str, list] = {name: [] for name in GROUPS}
    subgroups: dict[tuple[str, str], list[Page]] = {}
    for page in pages:
        if not page.group or page.slug == "index":
            continue
        if not page.subgroup:
            layout[page.group].append(page)
            continue
        key = (page.group, page.subgroup)
        if key not in subgroups:
            subgroups[key] = []
            layout[page.group].append((page.subgroup, subgroups[key]))
        subgroups[key].append(page)
    return layout


def ordered_pages(pages: list["Page"]) -> list["Page"]:
    """The pages in sidebar order: the order of previous and next."""
    layout = _layout(pages)
    flat: list[Page] = []
    for name in GROUPS:
        for entry in layout[name]:
            flat.extend(entry[1] if isinstance(entry, tuple) else [entry])
    return flat


def _side_link(page: "Page", current_slug: str) -> str:
    current = ' aria-current="page"' if page.slug == current_slug else ""
    return f'<li><a href="{html.escape(page.slug)}.html"{current}>{html.escape(page.title)}</a></li>'


def _sidebar_item(entry, current_slug: str) -> tuple[str, bool]:
    """The list item of one entry of a group (a page, or a subgroup and its pages) and whether it holds the current page."""
    if not isinstance(entry, tuple):
        return _side_link(entry, current_slug), entry.slug == current_slug
    sub_name, members = entry
    sub_open = any(member.slug == current_slug for member in members)
    open_attribute = " open" if sub_open else ""
    links = "".join(_side_link(member, current_slug) for member in members)
    return f'<li class="side-sub"><details{open_attribute}><summary>{html.escape(sub_name)}</summary><ul>{links}</ul></details></li>', sub_open


def render_sidebar(pages: list["Page"], current_slug: str) -> str:
    """The groups in their fixed order (empty ones left out) as native collapsible blocks; a subgroup nests one level deeper; the
    group and subgroup that hold the current page are open, and the current page is marked aria-current."""
    layout = _layout(pages)
    blocks = []
    for name in GROUPS:
        if not layout[name]:
            continue
        rendered = [_sidebar_item(entry, current_slug) for entry in layout[name]]
        open_attribute = " open" if any(holds for _, holds in rendered) else ""
        items = "".join(item for item, _ in rendered)
        blocks.append(f'<div class="side-group"><details{open_attribute}><summary>{html.escape(name)}</summary><ul>{items}</ul></details></div>')
    return "".join(blocks)


def _pager_link(page: "Page", side: str) -> str:
    label, rel = ("Previous", "prev") if side == "prev" else ("Next", "next")
    return f'<a class="pager-{rel}" rel="{rel}" href="{html.escape(page.slug)}.html"><span>{label}</span><strong>{html.escape(page.title)}</strong></a>'


def render_pager(pages: list["Page"], current_slug: str) -> str:
    """Links to the previous and the next page in sidebar order; empty for a page that is not in the sidebar (the home page)."""
    order = ordered_pages(pages)
    slugs = [page.slug for page in order]
    if current_slug not in slugs:
        return ""
    position = slugs.index(current_slug)
    links = []
    if position > 0:
        links.append(_pager_link(order[position - 1], "prev"))
    if position + 1 < len(order):
        links.append(_pager_link(order[position + 1], "next"))
    return f'<nav class="pager" aria-label="Previous and next">{"".join(links)}</nav>'


def render_toc(rendered: Rendered) -> str:
    """The 'On this page' block of the level-2 headings, or '' when there are none."""
    links = "".join(f'<li><a href="#{html.escape(anchor)}">{html.escape(text)}</a></li>' for level, text, anchor in rendered.headings if level == 2)
    return f'<p class="side-title">On this page</p><ul>{links}</ul>' if links else ""


HEAD_LINKS = ('<link rel="icon" type="image/svg+xml" href="img/snake.svg"><link rel="icon" href="img/favicon.ico" sizes="any">'
              '<link rel="apple-touch-icon" href="img/snake-180.png">')


SCRIPTS = ("theme.js", "site.js")
SCRIPT_TAGS = '<script src="theme.js"></script><script src="site.js" defer></script>'
_ICON = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>'
ICON_GITHUB = ('<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor" aria-hidden="true"><path d="M12 2a10 10 0 0 0-3.16 19.49c.5.09.68-.22.68-.48v-1.7'
               'c-2.78.6-3.37-1.34-3.37-1.34-.45-1.15-1.11-1.46-1.11-1.46-.91-.62.07-.61.07-.61 1 .07 1.53 1.03 1.53 1.03.89 1.52 2.34 1.08 2.9.83.09-.65.35-1.08.63-1.33'
               '-2.22-.25-4.55-1.11-4.55-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.27.1-2.64 0 0 .84-.27 2.75 1.02A9.5 9.5 0 0 1 12 6.8c.85 0 1.7.11 2.5.34 1.9-1.29 2.74-1.02 2.74-1.02'
               '.55 1.37.2 2.39.1 2.64.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.69-4.57 4.94.36.31.68.92.68 1.85v2.74c0 .27.18.58.69.48A10 10 0 0 0 12 2Z"/></svg>')
ICON_PACKAGE = _ICON.format('<path d="M21 8 12 3 3 8v8l9 5 9-5V8Z"/><path d="m3 8 9 5 9-5M12 13v8"/>')
ICON_SEARCH = _ICON.format('<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>')
ICON_MENU = _ICON.format('<path d="M4 6h16M4 12h16M4 18h16"/>')
ICON_THEME = ('<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
              '<g class="i-auto"><circle cx="12" cy="12" r="8"/><path d="M12 4v16" /></g><g class="i-light"><circle cx="12" cy="12" r="4"/>'
              '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></g>'
              '<g class="i-dark"><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5Z"/></g></svg>')
BRAND_IMAGE = '<img src="img/snake.svg" alt="" width="28" height="28">'
_HEADING_OPEN = re.compile(r'<h([234]) id="([^"]+)">(.*?)</h\1>', re.S)


def render_topbar(config: dict, pages: list["Page"]) -> str:
    """The top bar of every page: logo, Docs and Reference, the repository and the published registries, theme."""
    order = ordered_pages(pages)
    docs = order[0].slug if order else "index"
    reference = next((p.slug for p in order if p.group == "Reference"), docs)
    registries = "".join(
        f'<a class="icon-btn" href="{html.escape(url, quote=True)}" aria-label="{html.escape(key)} package">{ICON_PACKAGE}</a>'
        for key, url in config.get("packages", {}).items())
    return (f'<header class="top"><div class="wrap-wide"><a class="brand" href="index.html">{BRAND_IMAGE}PLURIES VENOM</a>'
            f'<nav class="top-nav" aria-label="Main"><a href="{html.escape(docs)}.html">Docs</a><a href="{html.escape(reference)}.html">Reference</a></nav>'
            f'<div class="top-actions"><a class="icon-btn" href="{html.escape(config["repo"], quote=True)}" aria-label="GitHub repository">{ICON_GITHUB}</a>{registries}'
            f'<button type="button" class="icon-btn search-button" aria-label="Search the documentation" hidden>{ICON_SEARCH}</button>'
            f'<button type="button" class="icon-btn theme-button" aria-label="Theme: Auto. Change theme">{ICON_THEME}</button></div></div></header>')


def render_group_cards(config: dict, pages: list["Page"]) -> str:
    """One card per group that has pages, linking its first page; empty without a `groups` key in site.json. A group that has pages
    and no description fails the build."""
    groups = config.get("groups")
    if groups is None:
        return ""
    order = ordered_pages(pages)
    cards = []
    for name in GROUPS:
        first = next((p for p in order if p.group == name), None)
        if first is None:
            continue
        description = groups.get(name)
        if not isinstance(description, str) or not description.strip():
            raise BuildError(f"site/site.json: the group '{name}' has pages and needs a one-sentence description under 'groups'")
        cards.append(f'<a class="card" href="{html.escape(first.slug)}.html"><h3>{html.escape(name)}</h3><p>{html.escape(description)}</p></a>')
    return f'<div class="cards group-cards">{"".join(cards)}</div>'


def render_menu_button(slug: str) -> str:
    """The button that opens the sidebar on a phone; the home page has no sidebar."""
    if slug == "index":
        return ""
    return f'<button type="button" class="menu-toggle" aria-controls="sidebar" aria-expanded="false" aria-label="Open the page menu">{ICON_MENU}<span>Menu</span></button>'


def add_anchors(page_html: str) -> str:
    """A link anchor in each heading of level 2 to 4, shown on hover and on keyboard focus."""
    return _HEADING_OPEN.sub(lambda m: (f'<h{m.group(1)} id="{m.group(2)}">{m.group(3)} '
                                        f'<a class="anchor" href="#{m.group(2)}" aria-label="Link to this section">#</a></h{m.group(1)}>'), page_html)


@dataclass
class LinkContext:
    pages_by_slug: dict[str, Page] | None = None
    page_anchors: "Callable[[str], set[str]] | None" = None


def normalise_base_url(value: object, where: str) -> str:
    """The public address of the site: https, a host, no credentials, no query, no fragment; one trailing slash is dropped."""
    if not isinstance(value, str):
        raise BuildError(f"{where}: baseUrl must be an https address")
    address = value[:-1] if value.endswith("/") else value
    try:
        parts = urllib.parse.urlsplit(address)
    except ValueError as error:
        raise BuildError(f"{where}: baseUrl '{value}' is not an address") from error
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc or parts.query or parts.fragment or address.endswith("/") or re.search(r"\s", address):
        raise BuildError(f"{where}: baseUrl '{value}' must be an https address without credentials, query or fragment")
    return address


def _is_private(rel: str) -> bool:
    """True for a path the public mirror does not carry: a PRIVATE entry itself (file or bare directory) or anything under it.
    Compared case-insensitively: the check is by name, so it holds on a case-sensitive file system too."""
    rel = rel.strip("/").casefold()
    return any(rel == entry.casefold() or rel.startswith(entry.casefold() + "/") for entry in PRIVATE_PATHS)


def _normal(path: str) -> str:
    return os.path.normpath(path).replace(os.sep, "/")


OUTSIDE, PRIVATE = "outside", "private"


def _spelling_problem(path: str) -> str | None:
    """Absolute and network paths leave the repository before anything touches the disk; a segment the file system may read
    as another name (a drive letter or an NTFS stream after a colon, a trailing dot or space) cannot be vouched for."""
    if path.startswith(("/", "\\")) or os.path.isabs(path) or PureWindowsPath(path).drive:
        return OUTSIDE
    normal = _normal(path)
    if normal == ".." or normal.startswith("../"):
        return OUTSIDE
    if any(":" in part or (part != "." and part.endswith((".", " "))) for part in normal.split("/")):
        return PRIVATE
    return PRIVATE if _is_private(normal) else None


def path_problem(root: Path, path: object) -> str | None:
    """OUTSIDE, PRIVATE or None for a path the build is about to read or link. The name is tested first, then the real path of
    the file when it exists: the file system decides what a spelling reads, and a symlink or junction leads anywhere."""
    if not isinstance(path, str) or not path:
        return OUTSIDE
    problem = _spelling_problem(path)
    if problem:
        return problem
    try:
        inside = (root / _normal(path)).resolve().relative_to(root.resolve())
    except ValueError:
        return OUTSIDE
    except OSError:
        return PRIVATE
    return PRIVATE if _is_private(inside.as_posix()) else None


def _check_site_keys(config: object, path: Path) -> dict:
    if not isinstance(config, dict):
        raise BuildError(f"{path}: the file must hold one JSON object")
    for key in ("title", "repo", "branch", "pages"):
        if key not in config:
            raise BuildError(f"{path}: missing '{key}'")
    packages = config.setdefault("packages", {})
    if not isinstance(packages, dict) or not all(isinstance(k, str) and isinstance(v, str) and v.startswith("https://") for k, v in packages.items()):
        raise BuildError(f"{path}: 'packages' must map a registry name to an https address")
    return config


def _check_groups(groups: object, path: Path) -> None:
    if not isinstance(groups, dict):
        raise BuildError(f"{path}: 'groups' must map a group name to a one-sentence description")
    for name, text in groups.items():
        if name not in GROUPS:
            raise BuildError(f"{path}: 'groups': '{name}' is not one of {', '.join(GROUPS)}")
        if not isinstance(text, str):
            raise BuildError(f"{path}: 'groups': the description of '{name}' must be a string")


def _check_placement(entry: dict, path: Path) -> None:
    slug = entry["slug"]
    if not SLUG.match(slug):
        raise BuildError(f"{path}: page slug '{slug}' must match [a-z0-9][a-z0-9-]*")
    group = entry.get("group")
    if group not in GROUPS and not (slug == "index" and group is None):
        raise BuildError(f"{path}: page '{slug}' needs a 'group' that is one of {', '.join(GROUPS)}")
    subgroup = entry.get("subgroup")
    if subgroup is not None and (not isinstance(subgroup, str) or not subgroup.strip()):
        raise BuildError(f"{path}: page '{slug}': 'subgroup' must be a non-empty string")
    description = entry.get("description")
    if not isinstance(description, str) or not description.strip():
        raise BuildError(f"{path}: page '{slug}' needs a one-sentence 'description'")


def _check_files(entry: dict, root: Path, path: Path) -> None:
    slug = entry["slug"]
    template = entry.get("template")
    if template is not None:
        if path_problem(root, template):
            raise BuildError(f"{path}: page '{slug}': template {template!r} must be a public file inside the repository")
        if not (root / _normal(template)).is_file():
            raise BuildError(f"{path}: page '{slug}': template '{template}' does not exist")
    if path_problem(root, entry.get("source")):
        raise BuildError(f"{path}: page '{slug}': source {entry.get('source')!r} must be a public file inside the repository")


def _check_page(entry: object, root: Path, path: Path) -> None:
    if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in ("slug", "title")):
        raise BuildError(f"{path}: every page needs a string 'slug' and 'title'")
    _check_placement(entry, path)
    _check_files(entry, root, path)


def _readme_split(root: Path, entry: dict) -> list[dict]:
    try:
        return site_readme_split.expand(root, entry)
    except site_readme_split.SplitError as error:
        raise BuildError(str(error)) from error


GENERATORS: dict[str, Callable[[Path, dict], list[dict]]] = {"executors": site_executors.expand, "readme-split": _readme_split}


def _expand_generators(pages: list, root: Path, path: Path) -> list:
    """Replace each `{"generator": ...}` entry by the ordinary page entries its generator produces."""
    expanded: list = []
    for entry in pages:
        if not isinstance(entry, dict) or "generator" not in entry:
            expanded.append(entry)
            continue
        name = entry["generator"]
        if name not in GENERATORS:
            raise BuildError(f"{path}: unknown generator {name!r}; known: {', '.join(GENERATORS)}")
        if "slug" in entry or ("source" in entry and name != "readme-split"):
            raise BuildError(f"{path}: generator entry {name!r} must not carry a 'slug' or 'source'")
        expanded.extend(GENERATORS[name](root, entry))
    return expanded


def load_config(root: Path) -> dict:
    path = root / "site" / "site.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BuildError(f"cannot read {path}: {error}") from error
    config = _check_site_keys(raw, path)
    if "groups" in config:
        _check_groups(config["groups"], path)
    if "baseUrl" in config:
        config["baseUrl"] = normalise_base_url(config["baseUrl"], str(path))
    config["pages"] = _expand_generators(config["pages"], root, path)
    for entry in config["pages"]:
        _check_page(entry, root, path)
    slugs = [p["slug"] for p in config["pages"]]
    if len(set(slugs)) != len(slugs) or "index" not in slugs:
        raise BuildError(f"{path}: page slugs must be unique and include 'index'")
    return config


_INCLUDE = re.compile(r"^\{\{include\s+(\S+)\s+([\w-]*)\}\}\s*$")


def _track_fence(line: str, fence: str | None) -> str | None:
    """The fence marker still open after `line`: the page's own fences hide an `{{include}}` example from the expansion."""
    opener = _FENCE_LINE.match(line)
    if not opener:
        return fence
    marker = opener.group(1)
    if fence is None:
        return marker
    return None if marker[0] == fence[0] and len(marker) >= len(fence) else fence


def _include_block(line: str, number: int, root: Path, source: str) -> str:
    match = _INCLUDE.match(line)
    if not match:
        raise BuildError(f"{source}:{number}: malformed include (expected `{{{{include <path> <language>}}}}`): {line[:60]}")
    if path_problem(root, match.group(1)):
        raise BuildError(f"{source}:{number}: include '{match.group(1)}' must be a public file inside the repository")
    rel = _normal(match.group(1))
    try:
        body = (root / rel).read_text(encoding="utf-8").replace("\r\n", "\n").rstrip("\n")
    except (OSError, UnicodeDecodeError) as error:
        raise BuildError(f"{source}:{number}: cannot include '{rel}': {error}") from error
    longest = max((len(run) for run in re.findall(r"`+", body)), default=0)
    ticks = "`" * max(4, longest + 1)
    return f"{ticks}{match.group(2)}\n{body}\n{ticks}"


def expand_includes(text: str, root: Path, source: str) -> str:
    """Replace each `{{include path language}}` line by the file as a fenced code block, the fence one backtick longer than the
    longest backtick run in the file (at least four), so a file that holds a fence of its own survives. A line that starts with
    `{{include` but is not well formed, and an unreadable, private or outside path, are configuration errors (exit 2). A line
    inside a code fence of the page itself is left alone (it is an example of the directive)."""
    out: list[str] = []
    fence: str | None = None
    for number, line in enumerate(text.split("\n"), 1):
        fence = _track_fence(line, fence)
        if fence is None and line.startswith("{{include"):
            out.append(_include_block(line, number, root, source))
        else:
            out.append(line)
    return "\n".join(out)


def _anchor_in(path_part: str, fragment: str, where: str, errors: list[str], page_anchors) -> None:
    if fragment and page_anchors is not None and fragment not in page_anchors(path_part[:-5]):
        errors.append(f"{where}: dead anchor '#{fragment}' in page {path_part}")


def _repo_href(config: dict, kind: str, target: str, fragment: str) -> str:
    return f"{config['repo'].rstrip('/')}/{kind}/{config['branch']}/{target.rstrip('/')}" + (f"#{fragment}" if fragment else "")


def _external_link(href: str, where: str, errors: list[str]) -> str:
    if not href.lower().startswith(ALLOWED_SCHEMES):
        errors.append(f"{where}: link '{href[:40]}' uses a scheme other than http, https or mailto")
    return href


def _own_anchor_link(href: str, fragment: str, own_anchors: set[str], where: str, errors: list[str]) -> str:
    if fragment and fragment not in own_anchors:
        errors.append(f"{where}: dead anchor '#{fragment}'")
    return href


def _is_built_page_link(path_part: str, context: "LinkContext") -> bool:
    return bool(context.pages_by_slug) and path_part.endswith(".html") and "/" not in path_part and path_part[:-5] in context.pages_by_slug


def _page_link(page: Page, target: str, fragment: str, where: str, errors: list[str], anchors_of) -> str:
    if fragment and fragment not in anchors_of(page.source):
        errors.append(f"{where}: dead anchor '#{fragment}' in {target}")
    return f"{page.slug}.html" + (f"#{fragment}" if fragment else "")


def _file_link(target: str, fragment: str, where: str, errors: list[str], root: Path, config: dict, anchors_of, raw: bool) -> str:
    if fragment and target.endswith(".md") and fragment not in anchors_of(target):
        errors.append(f"{where}: dead anchor '#{fragment}' in {target}")
    kind = "tree" if (root / target).is_dir() else ("raw" if raw else "blob")
    return _repo_href(config, kind, target, fragment)


def resolve_link(href: str, source: str, pages_by_source: dict[str, Page], root: Path, config: dict,
                 anchors_of, errors: list[str], line: int, own_anchors: set[str], raw: bool = False,
                 context: "LinkContext | None" = None) -> str:
    """The href to emit for `href` found in `source`, or the original one after recording an error."""
    where = f"{source}:{line}"
    context = context or LinkContext()
    if "\x00" in href:
        errors.append(f"{where}: a link target must be plain text, without a code span")
        return href
    if EXTERNAL.match(href):
        return _external_link(href, where, errors)
    path_part, _, fragment = href.partition("#")
    if not path_part:
        return _own_anchor_link(href, fragment, own_anchors, where, errors)
    if _is_built_page_link(path_part, context):
        _anchor_in(path_part, fragment, where, errors, context.page_anchors)
        return href
    joined = _repo_path(root, source, path_part)
    target = _normal(joined)
    problem = _target_problem(href, joined, target, root)
    if problem:
        errors.append(f"{where}: {problem}")
        return href
    page = pages_by_source.get(target)
    if page is not None:
        return _page_link(page, target, fragment, where, errors, anchors_of)
    return _file_link(target, fragment, where, errors, root, config, anchors_of, raw)


def _repo_path(root: Path, source: str, path_part: str) -> str:
    """`path_part` as found in `source`, relative to the repository root. A leading `/` means the repository root, but only when
    that file exists there: any other absolute path is left as it is and refused as leaving the repository."""
    if path_part.startswith("/") and not path_part.startswith(("//", "/\\")):
        rooted = path_part.lstrip("/")
        try:
            if rooted and (root / rooted).exists():
                return rooted
        except (OSError, ValueError):
            pass
    return os.path.join(os.path.dirname(source), path_part)


def _target_problem(href: str, joined: str, target: str, root: Path) -> str | None:
    """Why a repository path cannot be linked (outside the repository, private, or missing), or None."""
    problem = path_problem(root, joined)
    if problem == OUTSIDE:
        return f"link '{href}' leaves the repository"
    if problem == PRIVATE:
        return f"link '{href}' points into the private part of the repository ({target})"
    return None if (root / target).exists() else f"dead link '{href}' (no such file: {target})"


def fill_template(template: str, values: dict[str, str]) -> str:
    """One pass: a value that itself contains {{...}} (page content may) is never substituted again."""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), template)


def _write(path: Path, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


SEARCH_SIZE_LIMIT = 1_000_000
_ANCHOR_LINK = re.compile(r'\s*<a class="anchor"[^>]*>#</a>')
_BLOCK_END = re.compile(r"</(?:p|li|h[1-6]|pre|tr|td|th|blockquote|ul|ol|table)>|<br ?/?>")
_TAG = re.compile(r"<[^>]+>")
_PRE_BLOCK = re.compile(r"(<pre\b.*?</pre>)", re.S)


def _flow_text(fragment: str) -> list[str]:
    text = html.unescape(_TAG.sub("", _BLOCK_END.sub("\n", fragment)))
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return [line for line in lines if line]


def _pre_text(block: str) -> list[str]:
    return html.unescape(_TAG.sub("", block)).strip("\n").splitlines()


def plain_text(page_html: str) -> str:
    """The words of a rendered page: no tags, one paragraph per line; the lines of a code block keep their indentation."""
    lines: list[str] = []
    for index, part in enumerate(_PRE_BLOCK.split(page_html)):
        lines += _pre_text(part) if index % 2 else _flow_text(part)
    return "\n".join(lines)


class SiteBuild:
    """One build of the site: the pages, what each links to, and the files written. Kept as methods so that no single function
    carries the whole build."""

    def __init__(self, root: Path, config: dict, base: str) -> None:
        self.root, self.config, self.base = root, config, base
        self.errors: list[str] = []
        self.text_cache: dict[str, str] = {}
        self.anchor_cache: dict[str, set[str]] = {}
        self.file_anchor_cache: dict[str, set[str]] = {}
        self.out_dir: Path | None = None
        self.copied: set[str] = set()
        self.pages = [self._page(entry) for entry in config["pages"]]
        self.pages_by_slug = {p.slug: p for p in self.pages}
        self.pages_by_source = {p.source: p for p in self.pages if p.text is None}
        self.anchor_maps = self._anchor_maps()

    @staticmethod
    def _page(entry: dict) -> Page:
        return Page(entry["slug"], entry["title"], entry["source"], group=entry.get("group"), template=entry.get("template"),
                    description=entry["description"], subgroup=entry.get("subgroup"), text=entry.get("text"),
                    heading_index=entry.get("heading_index"))

    def _anchor_maps(self) -> dict[str, dict[str, tuple[str, str]]]:
        """For each README cut into pages: GitHub's anchor over the whole file (a skipped part counts) -> (page slug, the anchor
        the page gives that heading). The ordinals come from the generator; a disagreement with the renderer is a build error."""
        maps: dict[str, dict[str, tuple[str, str]]] = {}
        wholes: dict[str, list] = {}
        for page in self.pages:
            if page.heading_index is None:
                continue
            if page.source not in wholes:
                wholes[page.source] = render_markdown(self.read_whole(page.source)).headings
            whole = wholes[page.source]
            own = render_markdown(page.text).headings
            if len(own) != len(page.heading_index) or any(
                    k >= len(whole) or whole[k][1] != mine[1] for k, mine in zip(page.heading_index, own)):
                raise BuildError(f"{page.source}: the headings of page '{page.slug}' do not match the README's own headings")
            target = maps.setdefault(page.source, {})
            for k, mine in zip(page.heading_index, own):
                target[whole[k][2]] = (page.slug, mine[2])
        return maps

    def read_whole(self, rel: str) -> str:
        return self.public(rel).read_text(encoding="utf-8-sig")

    def readme_anchor(self, page: Page, fragment: str, where: str, errors: list[str]) -> str:
        """A `#anchor` written in a README the site cut into pages: its target is on this page or on another one."""
        found = self.anchor_maps.get(page.source, {}).get(fragment)
        if found is None:
            errors.append(f"{where}: dead anchor '#{fragment}'")
            return f"#{fragment}"
        slug, anchor = found
        return f"#{anchor}" if slug == page.slug else f"{slug}.html#{anchor}"

    def local_image(self, page: Page, href: str, where: str, errors: list[str]) -> str:
        """The site-relative address of an image that is a public file of the repository, copied under the output folder; the
        original href after an error is recorded, with nothing copied."""
        joined = _repo_path(self.root, page.source, href.partition("#")[0])
        target = _normal(joined)
        problem = _target_problem(href, joined, target, self.root)
        if problem is None and not (self.root / target).is_file():
            problem = f"image '{href}' is not a file ({target})"
        if problem:
            errors.append(f"{where}: {problem}")
            return href
        if self.out_dir is not None and target not in self.copied:
            destination = self.out_dir / "images" / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(self.public(target).read_bytes())
            self.copied.add(target)
        return "images/" + urllib.parse.quote(target)

    def public(self, rel: str) -> Path:
        if path_problem(self.root, rel):
            raise BuildError(f"{rel} must be a public file inside the repository")
        return self.root / rel

    def read(self, rel: str) -> str:
        return self.public(rel).read_text(encoding="utf-8")

    def page_text(self, page: Page) -> str:
        """The Markdown of a page, its includes expanded."""
        if page.slug not in self.text_cache:
            self.text_cache[page.slug] = page.text if page.text is not None else expand_includes(
                self.read(page.source), self.root, page.source)
        return self.text_cache[page.slug]

    def page_anchors(self, slug: str) -> set[str]:
        if slug not in self.anchor_cache:
            try:
                self.anchor_cache[slug] = set(render_markdown(self.page_text(self.pages_by_slug[slug])).anchors)
            except (OSError, UnicodeDecodeError):
                self.anchor_cache[slug] = set()
        return self.anchor_cache[slug]

    def anchors_of(self, rel: str) -> set[str]:
        """Anchors of a Markdown file of the repository (a page's own are computed from its text)."""
        if rel in self.pages_by_source:
            return self.page_anchors(self.pages_by_source[rel].slug)
        if rel not in self.file_anchor_cache:
            try:
                self.file_anchor_cache[rel] = render_markdown(self.read(rel)).anchors
            except (OSError, UnicodeDecodeError):
                self.file_anchor_cache[rel] = set()
        return self.file_anchor_cache[rel]

    def write_assets(self, out_dir: Path) -> bool:
        """The files every page shares: stylesheet, scripts, fonts, images. A file removed from the sources must not linger in the
        output. Returns whether both scripts exist."""
        out_dir.mkdir(parents=True, exist_ok=True)
        self.out_dir = out_dir
        shutil.rmtree(out_dir / "images", ignore_errors=True)
        for stale in out_dir.glob("*.html"):
            stale.unlink()
        _write(out_dir / "style.css", self.read("site/style.css"))
        scripts_ready = all((self.root / "site" / "js" / name).is_file() for name in SCRIPTS)
        for name in SCRIPTS:
            source = self.root / "site" / "js" / name
            if source.is_file():
                (out_dir / name).write_bytes(self.public(f"site/js/{name}").read_bytes())
            elif (out_dir / name).exists():
                (out_dir / name).unlink()
        self.copy_fonts(out_dir)
        self.copy_images(out_dir)
        return scripts_ready

    def copy_fonts(self, out_dir: Path) -> None:
        fonts_in, fonts_out = self.root / "site" / "fonts", out_dir / "fonts"
        if fonts_out.is_dir():
            for stale_font in fonts_out.iterdir():
                if stale_font.is_file():
                    stale_font.unlink()
        if fonts_in.is_dir():
            fonts_out.mkdir(exist_ok=True)
            for font in sorted(fonts_in.iterdir()):
                if font.is_file():
                    (fonts_out / font.name).write_bytes(self.public(f"site/fonts/{font.name}").read_bytes())

    def copy_images(self, out_dir: Path) -> None:
        images_in, images_out = self.root / "site" / "img", out_dir / "img"
        if images_out.is_dir():
            for stale_image in images_out.iterdir():
                if stale_image.is_file():
                    stale_image.unlink()
        if images_in.is_dir():
            images_out.mkdir(exist_ok=True)
            for image in sorted(images_in.iterdir()):
                if image.is_file():
                    (images_out / image.name).write_bytes(self.public(f"site/img/{image.name}").read_bytes())

    def resolver_for(self, page: Page, own: set[str], link_errors: list[str], lines) -> Callable:
        context = LinkContext(self.pages_by_slug, self.page_anchors)

        def resolve(href: str, image: bool = False) -> str:
            line = next(lines, 0)
            where = f"{page.source}:{line}"
            if page.heading_index is not None and href.startswith("#") and not image:
                return self.readme_anchor(page, href[1:], where, link_errors) if len(href) > 1 else href
            if image and self.is_local(href):
                return self.local_image(page, href, where, link_errors)
            return resolve_link(href, page.source, self.pages_by_source, self.root, self.config, self.anchors_of, link_errors,
                                line, own, raw=image, context=context)
        return resolve

    @staticmethod
    def is_local(href: str) -> bool:
        return bool(href) and "\x00" not in href and not EXTERNAL.match(href) and not href.startswith("#")

    def render(self, page: Page, text: str) -> Rendered:
        """The page's HTML body, links resolved against what the site builds. The first pass finds the links' lines; the second
        resolves them, now that the page's own anchors are known."""
        link_errors: list[str] = []
        first = render_markdown(text)
        resolver = self.resolver_for(page, self.page_anchors(page.slug), link_errors, iter(line for line, _ in first.links))
        rendered = render_markdown(text, resolve=resolver)
        self.errors.extend(link_errors)
        page.rendered = rendered
        return rendered

    def shared_values(self) -> dict[str, str]:
        return {
            "site_title": html.escape(self.config["title"]),
            "head_links": HEAD_LINKS,
            "nav": "".join(f'<li><a href="{html.escape(p.slug)}.html">{html.escape(p.title)}</a></li>' for p in self.pages),
            "topbar": render_topbar(self.config, self.pages),
            "group_cards": render_group_cards(self.config, self.pages),
            "executor_count": str(self.executor_count()),
        }

    def executor_count(self) -> int:
        folder = self.root / "executors"
        if not folder.is_dir():
            return 0
        return sum(1 for p in folder.iterdir() if p.is_dir() and p.name != "plugins" and (p / "README.md").is_file())

    def page_values(self, page: Page, rendered: Rendered) -> dict[str, str]:
        return {
            "page_title": html.escape(page.title),
            "description": html.escape(page.description, quote=True),
            "sidebar": render_sidebar(self.pages, page.slug),
            "toc": render_toc(rendered),
            "prev_next": render_pager(self.pages, page.slug),
            "menu_button": render_menu_button(page.slug),
            "content": rendered.html.replace("{{", "{&#123;"),
            "source": html.escape(page.source),
            "source_url": html.escape(f"{self.config['repo'].rstrip('/')}/blob/{self.config['branch']}/{page.source}"),
        }

    def build_page(self, page: Page, out_dir: Path, templates: dict[str, str], shared: dict[str, str]) -> None:
        try:
            text = self.page_text(page)
        except (OSError, UnicodeDecodeError) as error:
            self.errors.append(f"site/site.json: page '{page.slug}': cannot read {page.source}: {error}")
            return
        rendered = self.render(page, text)
        rendered.html = add_anchors(rendered.html)
        values = {**shared, **self.page_values(page, rendered)}
        _write(out_dir / f"{page.slug}.html", fill_template(templates[page.template or "site/template.html"], values))

    def write_search_files(self, out_dir: Path) -> None:
        """Write search-index.json, llms.txt and llms-full.txt from self.pages and self.base; a missing base or a file over
        SEARCH_SIZE_LIMIT is a BuildError naming it."""
        if not self.base:
            raise BuildError("llms.txt needs the public base address of the site: set baseUrl in site/site.json or pass --base-url")
        entries = [self.search_entry(page) for page in self.pages if page.rendered is not None]
        index = json.dumps(entries, sort_keys=True, ensure_ascii=False, indent=1) + "\n"
        full = "\n\n".join(f"# {e['title']}\n\n{e['text']}" for e in entries) + "\n"
        over = [name for name, content in (("search-index.json", index), ("llms-full.txt", full))
                if len(content.encode("utf-8")) > SEARCH_SIZE_LIMIT]
        if over:
            raise BuildError(f"{', '.join(over)}: over the limit of {SEARCH_SIZE_LIMIT} bytes")
        _write(out_dir / "search-index.json", index)
        _write(out_dir / "llms.txt", self.llms_text())
        _write(out_dir / "llms-full.txt", full)

    @staticmethod
    def search_entry(page: Page) -> dict:
        body = _ANCHOR_LINK.sub("", page.rendered.html)
        return {
            "slug": page.slug, "title": page.title, "group": page.group or "", "description": page.description,
            "text": plain_text(body),
            "headings": [{"text": text, "anchor": anchor} for level, text, anchor in page.rendered.headings if 2 <= level <= 4],
        }

    def llms_text(self) -> str:
        title = self.config["title"]
        lines = [f"# {title}", ""]
        base = self.base.rstrip("/")
        lines += [f"- [{p.title}]({base}/{p.slug}.html): {p.description}" for p in self.pages]
        return "\n".join(lines) + "\n"

    def run(self, out_dir: Path) -> list[str]:
        templates = {rel: self.read(rel) for rel in {"site/template.html", *(p.template for p in self.pages if p.template)}}
        scripts_ready = self.write_assets(out_dir)
        shared = {**self.shared_values(), "scripts": SCRIPT_TAGS if scripts_ready else ""}
        for page in self.pages:
            self.build_page(page, out_dir, templates, shared)
        self.write_search_files(out_dir)
        return self.errors


PRIVATE_NAMES_FILE = "docs/compliance/site-private-names.txt"


def private_names(root: Path) -> tuple[str, ...]:
    """Host and organisation names the output must not carry, one per line, from a file the public mirror does not have."""
    path = root / PRIVATE_NAMES_FILE
    if not path.is_file():
        return ()
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return tuple(line for line in lines if line and not line.startswith("#"))


def build(root: Path, out_dir: Path, base_url: str | None = None) -> list[str]:
    """Build the site; returns the list of errors (empty when every link resolves)."""
    config = load_config(root)
    base = normalise_base_url(base_url, "--base-url") if base_url is not None else config.get("baseUrl", "")
    errors = SiteBuild(root, config, base).run(out_dir)
    host = urllib.parse.urlsplit(base).hostname or "" if base else ""
    return errors + site_checks.check_output(out_dir, host, PRIVATE_PATHS, private_names(root))


def _say(message: str, stream) -> None:
    """Write a line a console of any code page can show: what it cannot encode becomes a replacement character."""
    encoding = getattr(stream, "encoding", None) or "utf-8"
    stream.write(message.encode(encoding, errors="replace").decode(encoding, errors="replace") + "\n")


def main(argv: list[str] | None = None) -> int:
    default_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Build the venom docs site (see the module docstring).")
    parser.add_argument("--root", type=Path, default=default_root, help="repository root (default: this checkout)")
    parser.add_argument("--out", type=Path, default=None, help="output folder (default: <root>/site/_build)")
    parser.add_argument("--base-url", default=None, help="the public https address of the site (default: baseUrl of site/site.json)")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    out_dir = (args.out or root / "site" / "_build").resolve()
    try:
        errors = build(root, out_dir, args.base_url)
    except (BuildError, OSError, UnicodeDecodeError) as error:
        _say(f"build_site: {error}", sys.stderr)
        return 2

    if errors:
        for error in errors:
            _say(f"build_site: {error}", sys.stderr)
        _say(f"build_site: {len(errors)} problem(s); the site was written to {out_dir} but must not be published.", sys.stderr)
        return 1

    _say(f"build_site: built {len(list(out_dir.glob('*.html')))} page(s) into {out_dir}", sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
