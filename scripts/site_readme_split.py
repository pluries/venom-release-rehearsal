"""The `readme-split` page generator of the docs site (story #3621, task #3636): cuts README.md into site pages by its headings."""
from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")


class SplitError(Exception):
    """A configuration problem of a `readme-split` entry (the build reports it as a BuildError)."""


class _Heading(NamedTuple):
    line: int
    level: int
    text: str
    ordinal: int  # position among every heading of the file; the build maps README-wide anchors with it


def _headings(lines: list[str]) -> list[_Heading]:
    """The headings outside code fences, found by the same rules the page renderer applies, so ordinals agree with its list."""
    found: list[_Heading] = []
    fence = None
    for number, line in enumerate(lines):
        if fence:
            if re.match(rf"^\s*{re.escape(fence[0])}{{{len(fence)},}}\s*$", line):
                fence = None
            continue
        opener = _FENCE.match(line)
        if opener:
            fence = opener.group(1)
            continue
        match = _HEADING.match(line)
        if match:
            found.append(_Heading(number, len(match.group(1)), match.group(2).strip(), len(found)))
    return found


def _field(item: object, key: str, where: str) -> str:
    if not isinstance(item, dict) or not isinstance(item.get(key), str):
        raise SplitError(f"{where}: every entry needs a string '{key}'")
    return item[key]


def _find(candidates: list[_Heading], text: str, where: str) -> _Heading:
    for heading in candidates:
        if heading.text == text:
            return heading
    raise SplitError(f"{where} '{text}'")


def _check_coverage(source: str, entry: dict, h1s: list[_Heading]) -> None:
    listed = [_field(item, "heading", source) for item in entry.get("sections", [])]
    skipped = list(entry.get("skip", []))
    for text in (*listed, *skipped):
        _find(h1s, text, f"{source} has no section")
    for heading in h1s:
        if heading.text not in listed and heading.text not in skipped:
            raise SplitError(f"{source} section '{heading.text}' has no page and is not skipped")


def _page(source: str, item: dict, parent: dict, heading: str, lines: list[str], line_numbers: list[int],
          headings: list[_Heading]) -> dict:
    inside = set(line_numbers)
    return {"slug": _field(item, "slug", source), "title": item.get("title", heading), "source": source,
            "group": item.get("group", parent.get("group")), "description": item.get("description", parent.get("description")),
            "text": "\n".join(lines).rstrip("\n") + "\n",
            "heading_index": [h.ordinal for h in headings if h.line in inside]}


def _split_blocks(source: str, item: dict, start: int, end: int, headings: list[_Heading]) -> list[tuple[dict, _Heading, int]]:
    """(split entry, its H2, the line where its block stops) for each listed H2 of the section at `start`..`end`."""
    shallow = [h for h in headings if start < h.line < end and h.level <= 2]
    twos = [h for h in shallow if h.level == 2]
    blocks = []
    for sub in item["split"]:
        text = _field(sub, "heading", source)
        two = _find(twos, text, f"{source} section '{item['heading']}' has no '##' heading")
        stop = next((h.line for h in shallow if h.line > two.line), end)
        blocks.append((sub, two, stop))
    return blocks


def _section_pages(source: str, lines: list[str], headings: list[_Heading], item: dict, start: int, end: int) -> list[dict]:
    heading = item["heading"]
    taken: set[int] = set()
    children = []
    blocks = _split_blocks(source, item, start, end, headings) if "split" in item else []
    for sub, two, stop in blocks:
        numbers = list(range(two.line, stop))
        taken.update(numbers)
        block = [lines[n] for n in numbers]
        block[0] = block[0][1:]
        children.append(_page(source, sub, item, two.text, block, numbers, headings))
    own = [n for n in range(start, end) if n not in taken]
    kept = [lines[n] for n in own]
    if "split" in item and not any(line.strip() for line in kept[1:]):
        return children
    return [_page(source, item, {}, heading, kept, own, headings), *children]


def expand(root: Path, entry: dict) -> list[dict]:
    """The page entries of a `{"generator": "readme-split", ...}` entry: slug, title, source, group, description and text."""
    source = entry.get("source", "README.md")
    try:
        text = (root / source).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise SplitError(f"cannot read {source}: {error}") from error
    lines = text.replace("\r\n", "\n").split("\n")
    headings = _headings(lines)
    h1s = [h for h in headings if h.level == 1]
    _check_coverage(source, entry, h1s)
    ends = {h.line: (h1s[i + 1].line if i + 1 < len(h1s) else len(lines)) for i, h in enumerate(h1s)}
    pages: list[dict] = []
    for item in entry.get("sections", []):
        start = _find(h1s, item["heading"], source).line
        pages.extend(_section_pages(source, lines, headings, item, start, ends[start]))
    return pages
