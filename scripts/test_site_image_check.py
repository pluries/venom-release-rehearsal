#!/usr/bin/env python3
"""Tests of the site image checks (story #3622, task #3662).
Findings start with a kind prefix: leak: diff: user: history: license:
Offline, standard library only. Run: python scripts/test_site_image_check.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import site_image_check as sc  # noqa: E402

HOST = "docs.example.test"
PRIVATE_PATHS = ("docs/compliance",)
NAME = "example-private.test"
CLEAN_PAGE = '<!doctype html><html><head><title>Venom</title></head><body><a href="/guide/">Guide</a></body></html>'
CLEAN_CONF = "worker_processes 1;\nevents {}\nhttp { server { listen 8080; root /usr/share/nginx/html; } }\n"


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_export(root: Path) -> None:
    write(root, sc.HTML_DIR + "/index.html", CLEAN_PAGE)
    write(root, sc.NGINX_CONF, CLEAN_CONF)
    write(root, sc.LICENSE_FILE, "Apache License 2.0\n")


class TempCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)


class T1LeakTests(TempCase):
    def scan(self, names=()):
        return sc.leak_scan(self.root, HOST, PRIVATE_PATHS, names)

    def setUp(self):
        super().setUp()
        make_export(self.root)

    def test_T1_clean_export_has_no_finding(self):
        self.assertEqual(self.scan((NAME,)), [])

    def assert_found(self, rel: str, findings: list[str]):
        self.assertTrue(findings, "no finding")
        self.assertTrue(all(f.startswith("leak:") for f in findings), findings)
        self.assertTrue(any(rel in f for f in findings), findings)

    def plant(self, rel: str, text: str, names=()):
        write(self.root, rel, text)
        return self.scan(names)

    def test_T1_private_path_in_page(self):
        rel = sc.HTML_DIR + "/index.html"
        self.assert_found(rel, self.plant(rel, CLEAN_PAGE.replace("Guide", "see docs/compliance/plan.md")))

    def test_T1_private_ip_in_page(self):
        rel = sc.HTML_DIR + "/index.html"
        self.assert_found(rel, self.plant(rel, CLEAN_PAGE.replace("Guide", "host 10.1.2.3")))

    def test_T1_dev_azure_in_page(self):
        rel = sc.HTML_DIR + "/index.html"
        self.assert_found(rel, self.plant(rel, CLEAN_PAGE.replace("Guide", "https://dev.azure.com/org")))

    def test_T1_private_name_in_page(self):
        rel = sc.HTML_DIR + "/index.html"
        self.assert_found(rel, self.plant(rel, CLEAN_PAGE.replace("Guide", NAME), (NAME,)))

    def test_T1_private_path_in_nginx_conf(self):
        self.assert_found(sc.NGINX_CONF, self.plant(sc.NGINX_CONF, CLEAN_CONF + "# docs/compliance/x\n"))

    def test_T1_private_ip_in_nginx_conf(self):
        self.assert_found(sc.NGINX_CONF, self.plant(sc.NGINX_CONF, CLEAN_CONF + "# upstream 192.168.1.5\n"))

    def test_T1_dev_azure_in_nginx_conf(self):
        self.assert_found(sc.NGINX_CONF, self.plant(sc.NGINX_CONF, CLEAN_CONF + "# dev.azure.com/org\n"))

    def test_T1_private_name_in_nginx_conf(self):
        self.assert_found(sc.NGINX_CONF, self.plant(sc.NGINX_CONF, CLEAN_CONF + f"# {NAME}\n", (NAME,)))

    def test_T1_private_name_ignored_when_not_given(self):
        self.assertEqual(self.plant(sc.NGINX_CONF, CLEAN_CONF + f"# {NAME}\n"), [])


class T2CompareTests(TempCase):
    def setUp(self):
        super().setUp()
        self.left = self.root / "left"
        self.right = self.root / "right"
        for side in (self.left, self.right):
            write(side, "index.html", "a")
            write(side, "guide/page.html", "b")

    def test_T2_equal_trees_have_no_difference(self):
        self.assertEqual(sc.compare_trees(self.left, self.right), [])

    def test_T2_changed_file(self):
        write(self.right, "guide/page.html", "changed")
        found = sc.compare_trees(self.left, self.right)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("diff:") and "guide/page.html" in found[0], found)

    def test_T2_one_byte_difference(self):
        (self.right / "index.html").write_bytes(b"b")
        self.assertEqual(len(sc.compare_trees(self.left, self.right)), 1)

    def test_T2_extra_file(self):
        write(self.right, "new/extra.html", "x")
        found = sc.compare_trees(self.left, self.right)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("diff:") and "new/extra.html" in found[0], found)

    def test_T2_missing_file(self):
        (self.right / "index.html").unlink()
        found = sc.compare_trees(self.left, self.right)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("diff:") and "index.html" in found[0], found)

    def test_T2_one_finding_each_kind(self):
        write(self.right, "guide/page.html", "changed")
        write(self.right, "extra.html", "x")
        (self.right / "index.html").unlink()
        self.assertEqual(len(sc.compare_trees(self.left, self.right)), 3)


def inspect(user) -> str:
    return json.dumps([{"Config": {"User": user}}])


class T3UserTests(unittest.TestCase):
    def test_T3_uid_and_gid_ok(self):
        self.assertEqual(sc.check_user(inspect("101:101")), [])

    def test_T3_uid_only_ok(self):
        self.assertEqual(sc.check_user(inspect("101")), [])

    def test_T3_parsed_list_and_object_accepted(self):
        self.assertEqual(sc.check_user([{"Config": {"User": "101"}}]), [])
        self.assertEqual(sc.check_user({"Config": {"User": "101"}}), [])

    def test_T3_bad_users_fail(self):
        for user in ("", "0", "0:0", "root", "nginx", "101:0"):
            with self.subTest(user=user):
                found = sc.check_user(inspect(user))
                self.assertTrue(found and all(f.startswith("user:") for f in found), found)

    def test_T3_missing_user_key_fails(self):
        found = sc.check_user(json.dumps([{"Config": {}}]))
        self.assertTrue(found and found[0].startswith("user:"), found)


class T4HistoryTests(unittest.TestCase):
    CLEAN = (
        "IMAGE CREATED CREATED BY SIZE\n"
        "abc 2 days ago USER 101:101 0B\n"
        "def 2 days ago COPY /out/ /usr/share/nginx/html/ # buildkit 2MB\n"
        "ghi 2 days ago EXPOSE 8080 0B\n"
    )

    def test_T4_clean_history_has_no_finding(self):
        self.assertEqual(sc.scan_history(self.CLEAN, (NAME,)), [])

    def test_T4_findings(self):
        cases = {
            "private ip": "RUN curl http://10.0.0.7/pkg",
            "private 192": "ENV PROXY=192.168.1.9",
            "azure": "RUN git clone https://dev.azure.com/org/p/_git/r",
            "password": "ENV password=hunter2",
            "token": "RUN login --token abcdef0123456789abcdef0123456789",
        }
        for label, line in cases.items():
            with self.subTest(label=label):
                found = sc.scan_history(self.CLEAN + line + "\n")
                self.assertTrue(found and all(f.startswith("history:") for f in found), found)

    def test_T4_private_name(self):
        found = sc.scan_history(self.CLEAN + f"RUN ping {NAME}\n", (NAME,))
        self.assertTrue(found and found[0].startswith("history:"), found)

    def test_T4_private_name_ignored_when_not_given(self):
        self.assertEqual(sc.scan_history(self.CLEAN + f"RUN ping {NAME}\n"), [])


class T5LicenseTests(TempCase):
    def test_T5_present_is_ok(self):
        write(self.root, sc.LICENSE_FILE, "Apache License 2.0\n")
        self.assertEqual(sc.check_license(self.root), [])

    def test_T5_missing_fails(self):
        found = sc.check_license(self.root)
        self.assertTrue(found and found[0].startswith("license:"), found)

    def test_T5_empty_fails(self):
        write(self.root, sc.LICENSE_FILE, "")
        found = sc.check_license(self.root)
        self.assertTrue(found and found[0].startswith("license:"), found)


if __name__ == "__main__":
    unittest.main()
