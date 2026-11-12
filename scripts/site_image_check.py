"""Checks of the exported site image (story #3622, task #3662): leak scan, tree comparison, user, history, licence.
Each function returns findings; an empty list means clean. Every finding starts with a lowercase kind and a colon:
leak: diff: user: history: license:"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import urllib.parse
from pathlib import Path

import build_site
import site_checks

HTML_DIR = "usr/share/nginx/html"
NGINX_CONF = "etc/nginx/nginx.conf"
LICENSE_FILE = "licenses/LICENSE"
LICENSE_DIR = "licenses"

USER_PATTERN = re.compile(r"(\d+)(?::(\d+))?")
SECRET_PATTERN = re.compile(r"\b(?:password|passwd|secret|token|api[_-]?key)\b[=:\s]+\S{4,}", re.I)


def _scan_dir(export_root: Path, rel_dir: str, base_host: str, private_paths, private_names) -> list[str]:
    found = site_checks.check_output(export_root / rel_dir, base_host, private_paths, private_names)
    return [f"leak: {rel_dir}/{finding}" for finding in found]


def _scan_conf(export_root: Path, base_host: str, private_paths, private_names) -> list[str]:
    conf = export_root / NGINX_CONF
    if not conf.is_file():
        return [f"leak: {NGINX_CONF}: missing"]
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copyfile(conf, Path(tmp) / "nginx.txt")
        found = site_checks.check_output(Path(tmp), base_host, private_paths, private_names)
    return [f"leak: {NGINX_CONF}{finding[len('nginx.txt'):]}" for finding in found]


def leak_scan(export_root: Path, base_host: str, private_paths: tuple[str, ...], private_names: tuple[str, ...] = ()) -> list[str]:
    """Scan HTML_DIR (with site_checks.check_output), the nginx config and licenses/ of the exported image.
    Finding: `leak: <path relative to export_root>: <what>`."""
    args = (base_host, private_paths, private_names)
    return (_scan_dir(export_root, HTML_DIR, *args) + _scan_conf(export_root, *args)
            + _scan_dir(export_root, LICENSE_DIR, *args))


def _files(root: Path) -> dict[str, Path]:
    return {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}


def compare_trees(left: Path, right: Path) -> list[str]:
    """Byte-for-byte comparison. Findings: `diff: changed <rel>`, `diff: extra <rel>` (only in right),
    `diff: missing <rel>` (only in left); rel is a posix relative path."""
    lefts, rights = _files(left), _files(right)
    findings = [f"diff: missing {rel}" for rel in sorted(lefts.keys() - rights.keys())]
    findings += [f"diff: extra {rel}" for rel in sorted(rights.keys() - lefts.keys())]
    findings += [f"diff: changed {rel}" for rel in sorted(lefts.keys() & rights.keys())
                 if lefts[rel].read_bytes() != rights[rel].read_bytes()]
    return findings


def _config_user(inspect_json: str | list | dict) -> object:
    data = json.loads(inspect_json) if isinstance(inspect_json, str) else inspect_json
    if isinstance(data, list):
        data = data[0]
    return data["Config"]["User"]


def check_user(inspect_json: str | list | dict) -> list[str]:
    """`docker inspect` output (JSON text, a list of one object, or the object): Config.User must be a numeric uid
    other than 0, with an optional numeric gid (`101`, `101:101`). Finding: `user: ...`."""
    try:
        user = _config_user(inspect_json)
    except (ValueError, KeyError, IndexError, TypeError):
        return ["user: Config.User not found in the inspect output"]
    match = USER_PATTERN.fullmatch(user) if isinstance(user, str) else None
    if not match:
        return [f"user: Config.User {user!r} is not a numeric uid[:gid]"]
    if int(match.group(1)) == 0 or int(match.group(2) or 1) == 0:
        return [f"user: Config.User {user!r} runs as root"]
    return []


def scan_history(text: str, private_names: tuple[str, ...] = ()) -> list[str]:
    """`docker history --no-trunc` text: a private host or address, a private name, a private-looking path
    or a secret-looking value gives a `history: ...` finding."""
    patterns = [("private host or address", site_checks.PRIVATE_HOSTS),
                ("private path", site_checks._private_path_regex(build_site.PRIVATE_PATHS)),
                ("secret-looking value", SECRET_PATTERN)]
    if private_names:
        patterns.append(("private host or address", re.compile("|".join(re.escape(n) for n in private_names), re.I)))
    findings: dict[str, None] = {}
    for what, pattern in patterns:
        for match in pattern.finditer(text):
            findings[f"history: {what}: {match.group(0)[:60]}"] = None
    return list(findings)


def check_license(export_root: Path) -> list[str]:
    """LICENSE_FILE present and non-empty. Finding: `license: ...`."""
    path = export_root / LICENSE_FILE
    if not path.is_file():
        return [f"license: {LICENSE_FILE} is missing"]
    if path.stat().st_size == 0:
        return [f"license: {LICENSE_FILE} is empty"]
    return []


def run_checks(args: argparse.Namespace, root: Path) -> list[str]:
    host = urllib.parse.urlsplit(build_site.load_config(root).get("baseUrl", "")).hostname or ""
    names = build_site.private_names(root)
    export = args.export
    findings = leak_scan(export, host, build_site.PRIVATE_PATHS, names)
    findings += check_user(args.inspect.read_text(encoding="utf-8"))
    findings += scan_history(args.history.read_text(encoding="utf-8"), names)
    findings += check_license(export)
    if args.compare_with:
        findings += compare_trees(export / HTML_DIR, args.compare_with)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the exported site image (see the module docstring).")
    parser.add_argument("--export", type=Path, required=True, help="root of the exported image filesystem")
    parser.add_argument("--inspect", type=Path, required=True, help="file holding `docker inspect` JSON")
    parser.add_argument("--history", type=Path, required=True, help="file holding `docker history --no-trunc` output")
    parser.add_argument("--compare-with", type=Path, default=None, help="built site folder the image content must equal")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2
    try:
        findings = run_checks(args, Path(__file__).resolve().parent.parent)
    except (build_site.BuildError, OSError, UnicodeDecodeError) as error:
        print(f"site-image-check: {error}", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding)
    print("site-image-check: PASS" if not findings else f"site-image-check: FAIL ({len(findings)} findings)")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
