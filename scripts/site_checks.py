"""Checks of the built docs site output (story #3621, task #3639): nothing private, nothing loaded from another host, no cookie
or tracking. Standard library only; `check_output` only reads the output folder."""
from __future__ import annotations

import html
import re
import urllib.parse
from pathlib import Path
from typing import Iterator

TEXT_SUFFIXES = frozenset({".html", ".css", ".js", ".svg", ".txt", ".json", ".xml"})
# Private entries that are one file, not a directory: they match as themselves, the others as `<dir>/<path run>`.
PRIVATE_FILES = frozenset({"CLAUDE.md", ".mcp.json", "Jenkinsfile", "sonar-project.properties", "scripts/publish-github.sh"})
PRIVATE_HOSTS = re.compile(
    r"dev\.azure\.com|visualstudio\.com"
    r"|(?<![\d.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|169\.254\.\d{1,3}\.\d{1,3})(?!\d)", re.I)

TAG = re.compile(r"""<([a-zA-Z][\w:-]*)\b((?:"[^"]*"|'[^']*'|[^<>"'])*)>""")
ATTR = re.compile(r"""([\w:.-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?""")
SCRIPT_BLOCK = re.compile(r"(<script\b[^>]*>)(.*?)</script>", re.I | re.S)
STYLE_BLOCK = re.compile(r"(<style\b[^>]*>)(.*?)</style>", re.I | re.S)
JS_ABSOLUTE_URL = re.compile(r"""["'`]((?:https?:)?//[^/"'`\s]+[^"'`\s]*)""")
CSS_URL = re.compile(r"""url\(\s*(["']?)(.*?)\1\s*\)""", re.I | re.S)
CSS_IMPORT = re.compile(r"""@import\s+(["'])(.*?)\1""", re.I)
EVENT_ATTR = re.compile(r"on[a-z]+")
META_REFRESH_URL = re.compile(r"""url\s*=\s*['"]?([^'";]+)""", re.I)

JS_MARKERS = (
    ("cookie access", re.compile(r"document\.cookie")),
    ("beacon", re.compile(r"navigator\.sendBeacon|sendBeacon")),
    ("XMLHttpRequest", re.compile(r"XMLHttpRequest")),
    ("fetch of an absolute URL", re.compile(r"""fetch\(\s*["'`]\s*(?:https?:)?//""")),
    ("tracking call", re.compile(r"\b(?:fbq|gtag)\(")),
)
# Plain English words (plausible, segment) are judged only where code or a request lives, never in prose.
ANALYTICS = re.compile(r"google-analytics|googletagmanager|plausible|matomo|hotjar|segment", re.I)

Problem = tuple[str, str]


def _private_path_regex(private_paths: tuple[str, ...]) -> re.Pattern[str]:
    parts = [rf"{re.escape(name)}(?![\w-])" if name in PRIVATE_FILES else rf"{re.escape(name)}/[\w./@-]*" for name in private_paths]
    return re.compile(r"(?:(?<![\w.\-/])|(?<=\./)|(?<=\.\./))(?:" + "|".join(parts) + ")")


def _foreign_host(url: str, base_host: str) -> bool:
    if not url.startswith(("//", "http://", "https://")):
        return False
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return True
    return host != base_host.lower()


def _url_problem(value: str, base_host: str) -> Problem | None:
    url = html.unescape(value).strip()
    low = url.lower().replace("\\", "/")
    if low.startswith("javascript:"):
        return "javascript: URL", url[:60]
    if low.startswith("data:"):
        mime = low.split(";")[0].split(",")[0]
        return None if mime.startswith("data:image/") else ("data: URL", mime)
    if _foreign_host(low, base_host):
        return "request to another host", url[:80]
    return None


def _css_problems(css: str, base_host: str) -> Iterator[Problem]:
    urls = [m.group(2) for m in CSS_URL.finditer(css)] + [m.group(2) for m in CSS_IMPORT.finditer(css)]
    for url in urls:
        problem = _url_problem(url, base_host)
        if problem:
            yield problem


def _requested_urls(tag: str, attr: str, value: str, attrs: dict[str, str]) -> list[str]:
    if attr == "srcset":
        return [candidate.split()[0] for candidate in value.split(",") if candidate.split()]
    if attr == "ping":
        return value.split()
    if attr in ("src", "action", "data", "poster", "formaction") or (attr in ("href", "xlink:href") and tag in ("link", "base", "image", "use")):
        return [value]
    if attr == "content" and attrs.get("http-equiv", "").lower() == "refresh":
        match = META_REFRESH_URL.search(value)
        return [match.group(1)] if match else []
    return []


def _attr_problems(tag: str, attr: str, value: str, attrs: dict[str, str], base_host: str) -> Iterator[Problem]:
    if EVENT_ATTR.fullmatch(attr):
        yield "inline event handler", attr
    decoded = html.unescape(value).strip()
    if decoded.lower().startswith("javascript:"):
        yield "javascript: URL", decoded[:60]
        return
    for url in _requested_urls(tag, attr, decoded, attrs):
        problem = _url_problem(url, base_host)
        if problem:
            yield problem
        found = ANALYTICS.search(url)
        if found:
            yield "analytics marker", found.group(0)
    if attr == "style":
        yield from _css_problems(decoded, base_host)


def _tag_problems(tag: str, raw_attrs: str, base_host: str) -> Iterator[Problem]:
    attrs = {m.group(1).lower(): html.unescape(m.group(2) or m.group(3) or m.group(4) or "") for m in ATTR.finditer(raw_attrs)}
    if tag.lower() == "meta" and attrs.get("http-equiv", "").lower() == "set-cookie":
        yield "cookie set by a meta tag", "set-cookie"
    for attr, value in attrs.items():
        yield from _attr_problems(tag.lower(), attr, value, attrs, base_host)


def _js_problems(code: str, with_analytics: bool, base_host: str = "") -> Iterator[Problem]:
    for what, pattern in JS_MARKERS:
        for match in pattern.finditer(code):
            yield what, match.group(0)
    if with_analytics:
        for match in ANALYTICS.finditer(code):
            yield "analytics marker", match.group(0)
        for match in JS_ABSOLUTE_URL.finditer(code):
            if _foreign_host(match.group(1), base_host):
                yield "absolute URL in a script", match.group(1)[:80]


def _markup_problems(text: str, base_host: str) -> Iterator[Problem]:
    scripts: list[str] = []
    styles: list[str] = []

    def keep(bucket: list[str]):
        def collect(match: re.Match[str]) -> str:
            bucket.append(match.group(2))
            return match.group(1) + "</x>"
        return collect

    markup = STYLE_BLOCK.sub(keep(styles), SCRIPT_BLOCK.sub(keep(scripts), text))
    for match in TAG.finditer(markup):
        yield from _tag_problems(match.group(1), match.group(2), base_host)
    for style in styles:
        yield from _css_problems(style, base_host)
    yield from _js_problems(text, with_analytics=False)
    for script in scripts:
        for match in ANALYTICS.finditer(script):
            yield "analytics marker", match.group(0)


def _file_problems(suffix: str, text: str, base_host: str) -> Iterator[Problem]:
    if suffix in (".html", ".svg"):
        yield from _markup_problems(text, base_host)
    elif suffix == ".css":
        yield from _css_problems(text, base_host)
    elif suffix == ".js":
        yield from _js_problems(text, with_analytics=True, base_host=base_host)


def check_output(out_dir: Path, base_host: str, private_paths: tuple[str, ...], private_names: tuple[str, ...] = ()) -> list[str]:
    """Scan every text file of `out_dir` and return one finding per problem, each `"<relative path>: <what>: <offending text>"`;
    an empty list means the output is clean. `base_host` is the site's own host; `private_paths` names what the public mirror
    does not carry; `private_names` are further host or organisation names read from a private file, kept out of this source."""
    path_regex = _private_path_regex(private_paths) if private_paths else None
    names_regex = re.compile("|".join(re.escape(name) for name in private_names), re.I) if private_names else None
    findings: dict[str, None] = {}
    for path in sorted(out_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        rel = path.relative_to(out_dir).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        problems = [("private path", m.group(0).rstrip(".,")) for m in path_regex.finditer(text)] if path_regex else []
        problems += [("private host or address", m.group(0)) for m in PRIVATE_HOSTS.finditer(text)]
        if names_regex:
            problems += [("private host or address", m.group(0)) for m in names_regex.finditer(text)]
        problems += list(_file_problems(path.suffix.lower(), text, base_host))
        for what, offending in problems:
            findings[f"{rel}: {what}: {offending}"] = None
    return list(findings)
