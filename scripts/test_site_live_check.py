#!/usr/bin/env python3
"""Tests of the live check of the deployed site (story #3623, task #3678).
LiveCheck covers AC-1, AC-2, FR-01, FR-02, FR-07, SC-01: scripts/check-site.sh checks the home page and its marker,
the certificate lifetime, the http to https redirect, the absence of cookies and the Content-Security-Policy header.
A local http.server on 127.0.0.1 plays the site and a stub openssl plays the certificate. Offline, standard library
only. Run: python scripts/test_site_live_check.py
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_site import ROOT  # noqa: E402

BASH = shutil.which("bash")
CURL = shutil.which("curl")
GNU_DATE = False
if BASH:
    probe = subprocess.run([BASH, "-c", "date -d '+1 day' +%s"], capture_output=True, text=True)
    GNU_DATE = probe.returncode == 0
SCRIPT = ROOT / "scripts" / "check-site.sh"
HOST = "venom.example.test"
MARKER = "Pluries Venom"
PATH_PROBE = "/quickstart/"
CSP = "default-src 'none'; script-src 'self'"
# Task #3704: the page fetches its own search index and the text files are read as utf-8; one check line per file.
FILES = ("search-index.json", "llms.txt", "llms-full.txt")
TEXT_FILES = ("llms.txt", "llms-full.txt")
BASE_CHECKS = 5
PROBES = ("post", "git", "dir")
HEALTHY_OKS = BASE_CHECKS + len(FILES) + len(PROBES)
PROBE_KEYS = {"/.git/config": "git_status", "/fonts/": "dir_status"}

OPENSSL_STUB = """\
#!/usr/bin/env bash
case "$1" in
  s_client) echo "stub handshake"; exit 0 ;;
  x509) echo "notAfter=$STUB_NOT_AFTER"; exit 0 ;;
esac
echo "openssl stub: unexpected arguments: $*" >&2
exit 1
"""


def not_after(days: int) -> str:
    moment = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{moment:%b} {moment.day:2d} {moment:%H:%M:%S} {moment.year} GMT"


def default_files() -> dict:
    return {
        "/search-index.json": (200, "application/json", '[{"title": "Quickstart", "url": "/quickstart/"}]'),
        "/llms.txt": (200, "text/plain; charset=utf-8", "# Pluries Venom\n"),
        "/llms-full.txt": (200, "text/plain; charset=utf-8", "# Pluries Venom\n\nFull text.\n"),
    }


def default_config() -> dict:
    return {
        "files": default_files(),
        "home_status": 200,
        "home_body": f"<html><title>{MARKER}</title></html>",
        "home_headers": {"Content-Security-Policy": CSP},
        "redirect_status": 301,
        "redirect_headers": {"Location": f"https://{HOST}{PATH_PROBE}"},
        "post_status": 403,
        "git_status": 404,
        "dir_status": 403,
    }


def probe_response(status: int) -> tuple:
    return status, ("probe body" if status == 200 else ""), {}


def file_response(config: dict, path: str) -> tuple:
    status, content_type, body = config["files"][path]
    return status, body, ({"Content-Type": content_type} if content_type else {})


def stub_response(config: dict, path: str) -> tuple:
    if path == "/":
        return config["home_status"], config["home_body"], config["home_headers"]
    if path in config.get("files", {}):
        return file_response(config, path)
    if path in PROBE_KEYS:
        return probe_response(config[PROBE_KEYS[path]])
    return config["redirect_status"], "", config["redirect_headers"]


def make_handler(config: dict):
    class Handler(BaseHTTPRequestHandler):
        def answer(self, status: int, body: str, headers: dict):
            if status == 0:
                self.close_connection = True
                return
            payload = body.encode("utf-8")
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.answer(*stub_response(config, self.path))

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.answer(*probe_response(config["post_status"] if self.path == "/" else 404))

        def log_message(self, *args):
            pass

    return Handler


def write_stub(directory: Path, name: str, text: str) -> None:
    path = directory / name
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


class Result:
    def __init__(self, completed: subprocess.CompletedProcess):
        self.returncode = completed.returncode
        self.stdout = completed.stdout
        self.stderr = completed.stderr
        self.output = completed.stdout + completed.stderr
        self.lines = self.output.splitlines()
        self.ok_lines = [line for line in self.lines if line.startswith("ok - ")]
        self.fail_lines = [line for line in self.lines if line.startswith("FAIL - ")]


def run_check(config: dict, days: int = 60) -> Result:
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with tempfile.TemporaryDirectory() as tmp:
            stubs = Path(tmp) / "stubs"
            stubs.mkdir()
            write_stub(stubs, "openssl", OPENSSL_STUB)
            env = dict(os.environ)
            env["PATH"] = stubs.as_posix() + os.pathsep + env.get("PATH", "")
            env["STUB_NOT_AFTER"] = not_after(days)
            env["CHECK_SITE_HTTPS_URL"] = base
            env["CHECK_SITE_HTTP_URL"] = base
            env["CHECK_SITE_CERT_ENDPOINT"] = "127.0.0.1:443"
            completed = subprocess.run([BASH, SCRIPT.as_posix(), HOST], cwd=ROOT, env=env, capture_output=True,
                                       text=True, encoding="utf-8", timeout=60)
            return Result(completed)
    finally:
        server.shutdown()
        server.server_close()


@unittest.skipUnless(BASH and CURL and GNU_DATE, "bash, curl or GNU date is not available")
class LiveCheck(unittest.TestCase):
    """T1 to T4 of task #3678. Covers AC-1, AC-2, FR-01, FR-02, FR-07, SC-01."""

    def assert_single_failure(self, result: Result, pattern: str):
        self.assertEqual(result.returncode, 1, result.output)
        self.assertIn("check-site: FAIL", result.lines)
        self.assertNotIn("check-site: PASS", result.lines)
        self.assertEqual(len(result.fail_lines), 1, result.output)
        self.assertRegex(result.fail_lines[0], re.compile(pattern, re.IGNORECASE))
        self.assertEqual(len(result.ok_lines), HEALTHY_OKS - 1, result.output)

    def test_T1_a_healthy_site_passes(self):
        result = run_check(default_config())
        self.assertEqual(result.returncode, 0, result.output)
        self.assertEqual(result.fail_lines, [], result.output)
        self.assertEqual(len(result.ok_lines), HEALTHY_OKS, result.output)
        self.assertEqual(result.lines[-1], "check-site: PASS")

    def test_T2_a_302_instead_of_301_fails(self):
        config = default_config()
        config["redirect_status"] = 302
        self.assert_single_failure(run_check(config), r"301|redirect")

    def test_T2_a_location_that_drops_the_path_fails(self):
        config = default_config()
        config["redirect_headers"] = {"Location": f"https://{HOST}/"}
        self.assert_single_failure(run_check(config), r"location")

    def test_T2_a_location_to_another_host_fails(self):
        config = default_config()
        config["redirect_headers"] = {"Location": f"https://other.example.test{PATH_PROBE}"}
        self.assert_single_failure(run_check(config), r"location")

    def test_T3_a_certificate_20_days_away_fails(self):
        self.assert_single_failure(run_check(default_config(), days=20), r"certificate|expire|days")

    def test_T3_a_set_cookie_on_the_home_page_fails(self):
        config = default_config()
        config["home_headers"] = {"Content-Security-Policy": CSP, "Set-Cookie": "sid=1"}
        self.assert_single_failure(run_check(config), r"cookie")

    def test_T3_a_set_cookie_on_the_redirect_fails(self):
        config = default_config()
        config["redirect_headers"] = {"Location": f"https://{HOST}{PATH_PROBE}", "Set-Cookie": "sid=1"}
        self.assert_single_failure(run_check(config), r"cookie")

    def test_T3_a_home_page_without_the_marker_fails(self):
        config = default_config()
        config["home_body"] = "<html><title>Something else</title></html>"
        self.assert_single_failure(run_check(config), r"marker|home")

    def test_a_missing_content_security_policy_fails(self):
        config = default_config()
        config["home_headers"] = {}
        self.assert_single_failure(run_check(config), r"content-security-policy|csp")

    def test_all_checks_are_reported_after_the_first_failure(self):
        config = default_config()
        config["home_status"] = 500
        config["home_body"] = "broken"
        config["redirect_status"] = 302
        result = run_check(config, days=20)
        self.assertEqual(result.returncode, 1, result.output)
        self.assertGreaterEqual(len(result.fail_lines), 4, result.output)
        self.assertEqual(len(result.ok_lines) + len(result.fail_lines), HEALTHY_OKS, result.output)
        self.assertEqual(result.lines[-1], "check-site: FAIL")

    def lines_naming(self, result: Result, name: str) -> list[str]:
        return [line for line in result.lines if line.startswith(("ok - ", "FAIL - ")) and name in line]

    def test_3704_T2_a_stub_serving_the_three_files_passes_with_one_ok_line_per_file(self):
        result = run_check(default_config())
        self.assertEqual(result.returncode, 0, result.output)
        for name in FILES:
            with self.subTest(file=name):
                lines = self.lines_naming(result, name)
                self.assertEqual(len(lines), 1, result.output)
                self.assertTrue(lines[0].startswith("ok - "), lines[0])

    def test_3704_T2_a_missing_file_fails_and_names_it(self):
        for name in FILES:
            with self.subTest(file=name):
                config = default_config()
                del config["files"]["/" + name]
                result = run_check(config)
                self.assert_single_failure(result, re.escape(name))
                self.assertEqual(len(self.lines_naming(result, name)), 1, result.output)

    def test_3704_T2_a_wrong_status_fails_and_names_the_file(self):
        for name in FILES:
            with self.subTest(file=name):
                config = default_config()
                status, content_type, body = config["files"]["/" + name]
                config["files"]["/" + name] = (500, content_type, body)
                self.assert_single_failure(run_check(config), re.escape(name) + r".*500|500.*" + re.escape(name))

    def test_3704_T3_a_text_file_served_without_utf_8_fails(self):
        for name in TEXT_FILES:
            with self.subTest(file=name):
                config = default_config()
                config["files"]["/" + name] = (200, "text/plain", "# Pluries Venom\n")
                self.assert_single_failure(run_check(config), re.escape(name) + r".*utf-8|utf-8.*" + re.escape(name))

    def test_3704_T3_a_text_file_served_with_another_charset_fails(self):
        config = default_config()
        config["files"]["/llms.txt"] = (200, "text/plain; charset=iso-8859-1", "# Pluries Venom\n")
        self.assert_single_failure(run_check(config), r"llms\.txt")

    PROBE_CASES = (
        ("post", "post_status", "probe-post", "/"),
        ("git", "git_status", "probe-git", "/.git/config"),
        ("dir", "dir_status", "probe-dir", "/fonts/"),
    )

    def test_us3_as9_a_healthy_host_yields_one_ok_line_per_probe(self):
        """US3-AS9."""
        result = run_check(default_config())
        self.assertEqual(result.returncode, 0, result.output)
        self.assertEqual(len(result.ok_lines), HEALTHY_OKS, result.output)
        for _, _, label, _ in self.PROBE_CASES:
            with self.subTest(probe=label):
                lines = self.lines_naming(result, label)
                self.assertEqual(len(lines), 1, result.output)
                self.assertTrue(lines[0].startswith("ok - "), lines[0])

    def test_us3_as9_a_probe_answered_200_fails_and_names_the_path_and_the_status(self):
        """US3-AS9."""
        for _, key, label, path in self.PROBE_CASES:
            with self.subTest(probe=label):
                config = default_config()
                config[key] = 200
                result = run_check(config)
                self.assert_single_failure(result, re.escape(label) + r".*\b200\b")
                if path != "/":
                    self.assertIn(path, result.fail_lines[0])

    def test_us3_as9_any_non_2xx_status_passes(self):
        """US3-AS9."""
        for _, key, label, _ in self.PROBE_CASES:
            for status in (301, 403, 404, 405, 500):
                with self.subTest(probe=label, status=status):
                    config = default_config()
                    config[key] = status
                    result = run_check(config)
                    self.assertEqual(result.returncode, 0, result.output)
                    self.assertEqual(len(result.ok_lines), HEALTHY_OKS, result.output)

    def test_us3_as9_a_probe_that_cannot_complete_fails_like_the_other_checks(self):
        """US3-AS9."""
        for _, key, label, _ in self.PROBE_CASES:
            with self.subTest(probe=label):
                config = default_config()
                config[key] = 0
                self.assert_single_failure(run_check(config), re.escape(label))

    def test_T4_a_host_that_does_not_resolve_exits_two_and_claims_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            stubs = Path(tmp) / "stubs"
            stubs.mkdir()
            write_stub(stubs, "openssl", OPENSSL_STUB)
            env = {k: v for k, v in os.environ.items() if not k.startswith("CHECK_SITE_")}
            env["PATH"] = stubs.as_posix() + os.pathsep + env.get("PATH", "")
            env["STUB_NOT_AFTER"] = not_after(60)
            completed = subprocess.run([BASH, SCRIPT.as_posix(), "venom-unresolvable.invalid"], cwd=ROOT, env=env,
                                       capture_output=True, text=True, encoding="utf-8", timeout=120)
        result = Result(completed)
        self.assertEqual(result.returncode, 2, result.output)
        self.assertIn("check-site: cannot resolve venom-unresolvable.invalid", result.lines)
        self.assertNotIn("check-site: PASS", result.lines)
        self.assertNotIn("check-site: FAIL", result.lines)
        self.assertEqual(result.ok_lines, [], result.output)


if __name__ == "__main__":
    unittest.main()
