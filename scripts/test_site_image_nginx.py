#!/usr/bin/env python3
"""Tests of the nginx config and the .dockerignore of the site image (story #3622, task #3660).
They read the REAL files, so a missing file or a missing directive is an assertion failure naming it.
Offline, standard library only. Run: python scripts/test_site_image_nginx.py
"""
import re
import sys
import unittest
from fnmatch import fnmatchcase
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs  # noqa: E402
from test_build_site import ROOT  # noqa: E402

NGINX_CONF = "deploy/site/nginx.conf"
DOCKERIGNORE = ".dockerignore"
MUST_EXCLUDE = tuple(bs.PRIVATE_PATHS) + (".git", "build", "dist")
MUST_KEEP = ("site", "scripts", "executors", "README.md", "LICENSE")
SECRET_AND_LOG_PATHS = (
    ".env", ".env.local", "sub/.env", "x.pem", "a/b/x.pem", "x.pfx", "tests/grpc/pki/ca.key", "x.key",
    "secrets/a", "tests/pki_variables.yml", "venom.log", "a/b/c.log", "x.dump.json", "venom.linux-amd64",
)
KEPT_PATHS = (
    "dist/venom.linux-amd64", "site", "site/fonts/inter.woff2", "site/index.html", "scripts/build_site.py",
    "executors/http/http.go", "README.md", "LICENSE", "deploy", "deploy/site/nginx.conf",
)


def read_text(rel: str) -> str:
    path = ROOT / rel
    if not path.is_file():
        raise AssertionError(f"{rel} does not exist")
    return path.read_text(encoding="utf-8")


def directives(conf: str) -> list[str]:
    """The config without comments, one directive or block header per element, whitespace collapsed."""
    lines = (line.split("#", 1)[0] for line in conf.splitlines())
    text = " ".join(lines)
    return [re.sub(r"\s+", " ", part).strip() for part in re.split(r"[;{}]", text) if part.strip()]


def ignore_entries(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def entry_matches(entry: str, name: str) -> bool:
    return entry.rstrip("/").lstrip("/") == name and not entry.startswith("!")


def segments_match(pattern: list[str], parts: list[str]) -> bool:
    """Docker pattern segments against path segments: `**` spans zero or more segments, `*` and `?` stay in one."""
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(segments_match(pattern[1:], parts[i:]) for i in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], pattern[0]) and segments_match(pattern[1:], parts[1:])


def is_excluded(entries: list[str], path: str) -> bool:
    """Last matching pattern wins; patterns are anchored at the context root; a match on a parent directory covers everything under it."""
    parts = path.strip("/").split("/")
    excluded = False
    for entry in entries:
        negated = entry.startswith("!")
        pattern = (entry[1:] if negated else entry).strip().strip("/").split("/")
        if any(segments_match(pattern, parts[:depth]) for depth in range(1, len(parts) + 1)):
            excluded = not negated
    return excluded


def copy_sources(dockerfile: str) -> list[str]:
    """Source paths of every COPY / ADD that reads the build context (no --from, no URL)."""
    sources = []
    for line in re.sub(r"\\\r?\n", " ", dockerfile).splitlines():
        words = line.split()
        if not words or words[0].upper() not in ("COPY", "ADD"):
            continue
        flags = [w for w in words[1:] if w.startswith("--")]
        args = [w for w in words[1:] if not w.startswith("--")]
        if any(f.startswith("--from=") for f in flags):
            continue
        sources += [a for a in args[:-1] if not re.match(r"(?i)https?://", a)]
    return sources


class NginxConfTests(unittest.TestCase):
    def setUp(self):
        self.conf = read_text(NGINX_CONF)
        self.items = directives(self.conf)

    def test_T1_listens_on_8080(self):
        self.assertTrue(any(re.fullmatch(r"listen (\[::\]:)?8080( .*)?", d) for d in self.items), "no `listen 8080`")

    def test_T1_pid_under_tmp(self):
        self.assertTrue(any(re.fullmatch(r"pid /tmp/\S+", d) for d in self.items), "no `pid /tmp/...`")

    def test_T1_temp_paths_under_tmp(self):
        for name in ("client_body_temp_path", "proxy_temp_path", "fastcgi_temp_path", "uwsgi_temp_path", "scgi_temp_path"):
            with self.subTest(directive=name):
                self.assertTrue(any(re.fullmatch(name + r" /tmp/\S+.*", d) for d in self.items), f"no `{name} /tmp/...`")

    def test_T1_server_tokens_off(self):
        self.assertIn("server_tokens off", self.items)

    def test_T1_root_is_nginx_html(self):
        self.assertIn("root /usr/share/nginx/html", self.items)

    def test_T2_no_proxy_pass(self):
        self.assertNotIn("proxy_pass", self.conf)

    def test_T2_no_set_cookie(self):
        self.assertNotRegex(self.conf, r"(?i)set-cookie")

    def test_T2_no_foreign_host_or_url(self):
        self.assertEqual(re.findall(r"(?i)https?://\S+", self.conf), [])


def csp_policy(conf: str) -> dict[str, list[str]]:
    """The Content-Security-Policy header of the config as directive name -> source tokens."""
    found = re.search(r'add_header\s+Content-Security-Policy\s+"([^"]*)"', conf)
    if not found:
        raise AssertionError("no Content-Security-Policy header in the nginx config")
    policy = {}
    for part in found.group(1).split(";"):
        words = part.split()
        if words:
            policy[words[0].lower()] = words[1:]
    return policy


def css_url_sources(css: str) -> tuple[set[str], set[str]]:
    """The sources (`'self'`, `data:`, or the host) the url() values of a stylesheet need: (font, image)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    font_blocks = re.findall(r"@font-face\s*\{[^}]*\}", css)
    rest = css
    for block in font_blocks:
        rest = rest.replace(block, "")

    def sources(text: str) -> set[str]:
        found = set()
        for dq, sq, bare in re.findall(r"""url\(\s*(?:"([^"]*)"|'([^']*)'|([^)'"\s]*))""", text):
            value = dq or sq or bare
            if value.lower().startswith("data:"):
                found.add("data:")
            elif re.match(r"(?i)(https?:)?//", value):
                found.add(re.sub(r"(?i)^(https?:)?//([^/]+).*", r"\2", value))
            else:
                found.add("'self'")
        return found

    return sources("".join(font_blocks)), sources(rest)


def csp_problems(policy: dict[str, list[str]], font: set[str], image: set[str]) -> list[str]:
    """What the policy blocks of the sources the stylesheet needs, and what it opens too wide."""
    problems = []
    for directive, needed in (("font-src", font), ("img-src", image)):
        for source in sorted(needed):
            if source not in policy.get(directive, []):
                problems.append(f"{directive} does not allow {source}")
    for token in policy.get("img-src", []):
        if token not in ("'self'", "data:", "'none'"):
            problems.append(f"img-src is open to {token}")
    for directive, tokens in policy.items():
        for token in tokens:
            if token in ("'unsafe-inline'", "'unsafe-eval'"):
                problems.append(f"{directive} has {token}")
    return problems


class CspSearchTests(unittest.TestCase):
    """T1 of task #3704: the page fetches its own search index, nothing else opens up."""

    EXPECTED = {
        "default-src": ["'none'"], "script-src": ["'self'"], "style-src": ["'self'"], "font-src": ["'self'"],
        "img-src": ["'self'", "data:"], "base-uri": ["'none'"], "form-action": ["'none'"], "frame-ancestors": ["'none'"],
        "connect-src": ["'self'"],
    }

    def test_T1_connect_src_is_the_site_itself_and_nothing_wider(self):
        self.assertEqual(csp_policy(read_text(NGINX_CONF)).get("connect-src"), ["'self'"])

    def test_T1_the_whole_policy_is_the_expected_one(self):
        self.assertEqual(csp_policy(read_text(NGINX_CONF)), self.EXPECTED)


class CharsetTests(unittest.TestCase):
    def test_T5_utf_8_is_the_declared_charset(self):
        items = directives(read_text(NGINX_CONF))
        self.assertIn("charset utf-8", items)
        self.assertEqual([d for d in items if d.startswith("charset ")], ["charset utf-8"])


class CspMatchesStylesheetTests(unittest.TestCase):
    def setUp(self):
        self.policy = csp_policy(read_text(NGINX_CONF))
        self.font, self.image = css_url_sources(read_text("site/style.css"))

    def test_the_stylesheet_uses_a_data_image_and_same_origin_fonts(self):
        self.assertIn("data:", self.image)
        self.assertEqual(self.font, {"'self'"})

    def test_the_policy_allows_every_url_the_stylesheet_loads_and_nothing_third_party(self):
        self.assertEqual(csp_problems(self.policy, self.font, self.image), [])

    def test_img_src_allows_data_images_and_the_site_itself_only(self):
        self.assertEqual(sorted(self.policy.get("img-src", [])), ["'self'", "data:"])

    def test_the_checker_reports_a_blocked_data_image(self):
        policy = {"font-src": ["'self'"], "img-src": ["'self'"]}
        self.assertEqual(csp_problems(policy, {"'self'"}, {"data:"}), ["img-src does not allow data:"])

    def test_the_checker_reports_third_party_sources_and_unsafe_keywords(self):
        policy = {"font-src": ["'self'"], "img-src": ["'self'", "https:", "*", "cdn.example"],
                  "script-src": ["'unsafe-inline'"]}
        problems = csp_problems(policy, set(), set())
        self.assertEqual(problems, ["img-src is open to https:", "img-src is open to *",
                                    "img-src is open to cdn.example", "script-src has 'unsafe-inline'"])


class DockerignoreTests(unittest.TestCase):
    def setUp(self):
        self.entries = ignore_entries(read_text(DOCKERIGNORE))

    def test_T3_private_and_build_paths_are_excluded(self):
        for name in MUST_EXCLUDE:
            with self.subTest(path=name):
                self.assertTrue(any(entry_matches(e, name) for e in self.entries), f"{name} is not excluded")

    def test_T3_site_inputs_are_not_excluded(self):
        for name in MUST_KEEP:
            with self.subTest(path=name):
                self.assertFalse(any(entry_matches(e, name) for e in self.entries), f"{name} is excluded")

    def test_root_dockerfile_copy_sources_are_not_excluded(self):
        sources = copy_sources(read_text("Dockerfile"))
        self.assertTrue(sources, "no COPY or ADD source found in the root Dockerfile")
        for src in sources:
            with self.subTest(source=src):
                self.assertFalse(is_excluded(self.entries, src), f"{src} is excluded by .dockerignore but the root Dockerfile copies it")

    def test_secrets_and_logs_are_excluded_at_any_depth(self):
        for path in SECRET_AND_LOG_PATHS:
            with self.subTest(path=path):
                self.assertTrue(is_excluded(self.entries, path), f"{path} reaches the build stage")

    def test_site_inputs_and_the_cli_binary_stay_in_the_context(self):
        for path in KEPT_PATHS:
            with self.subTest(path=path):
                self.assertFalse(is_excluded(self.entries, path), f"{path} is excluded")


class IsExcludedHelperTests(unittest.TestCase):
    def test_double_star_prefix_matches_any_depth_including_root(self):
        for path in ("a.log", "a/b.log", "a/b/c.log"):
            self.assertTrue(is_excluded(["**/*.log"], path), path)

    def test_plain_pattern_is_anchored_at_the_root(self):
        self.assertTrue(is_excluded([".env"], ".env"))
        self.assertFalse(is_excluded([".env"], "sub/.env"))

    def test_leading_slash_anchors_and_negation_wins_when_last(self):
        self.assertTrue(is_excluded(["/v.bin"], "v.bin"))
        self.assertFalse(is_excluded(["/v.bin"], "dist/v.bin"))
        self.assertFalse(is_excluded(["dist", "!dist/v.bin"], "dist/v.bin"))


if __name__ == "__main__":
    unittest.main()
