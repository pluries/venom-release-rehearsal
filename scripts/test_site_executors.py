#!/usr/bin/env python3
"""Tests of scripts/site_executors.py and of the `generator` page entry of scripts/build_site.py (story #3621, task #3635).
Offline, standard library only. Run: python scripts/test_site_executors.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_site as bs  # noqa: E402
import site_executors  # noqa: E402
from test_build_site import INDEX, REPO, ROOT, TEMPLATE, write_files  # noqa: E402

GENERATOR = {"generator": "executors", "group": "Executors"}


def scratch(files: dict[str, str], pages: list[dict]) -> Path:
    """A scratch repository with the site files, `index.md` and the given files."""
    root = Path(tempfile.mkdtemp())
    config = {"title": "T", "repo": REPO, "branch": "main", "baseUrl": "https://docs.example.test", "pages": pages}
    write_files(root, {"site/site.json": json.dumps(config), "site/template.html": TEMPLATE, "site/style.css": "body{}",
                       "index.md": "# Home\n", **files})
    return root


def readme(name: str, body: str = "text\n") -> str:
    return f"# Venom - Executor {name}\n\n{body}"


class Expansion(unittest.TestCase):
    def test_each_readme_becomes_a_page_titled_without_the_prefix_and_sorted_by_name(self):
        root = scratch({"executors/web/README.md": readme("Web"), "executors/amqp/README.md": readme("AMQP"),
                        "executors/http/README.md": readme("HTTP")}, [INDEX])
        pages = site_executors.expand(root, GENERATOR)
        self.assertEqual([(p["slug"], p["title"], p["source"]) for p in pages],
                         [("amqp", "AMQP", "executors/amqp/README.md"), ("http", "HTTP", "executors/http/README.md"),
                          ("web", "Web", "executors/web/README.md")])

    def test_a_page_carries_the_group_and_a_deterministic_description(self):
        root = scratch({"executors/http/README.md": readme("HTTP")}, [INDEX])
        page = site_executors.expand(root, {"generator": "executors", "group": "Reference"})[0]
        self.assertEqual(page["group"], "Reference")
        self.assertEqual(page["description"], "How to use the HTTP executor in a venom test suite.")

    def test_the_group_defaults_to_executors(self):
        root = scratch({"executors/http/README.md": readme("HTTP")}, [INDEX])
        self.assertEqual(site_executors.expand(root, {"generator": "executors"})[0]["group"], "Executors")

    def test_the_real_repository_has_one_page_per_executor_readme(self):
        expected = sorted(p.parent.name for p in (ROOT / "executors").glob("*/README.md"))
        pages = site_executors.expand(ROOT, GENERATOR)
        extra = ["writing-executors"] if (ROOT / "executors" / "README.md").is_file() else []
        self.assertEqual(sorted(p["slug"] for p in pages), sorted(expected + extra))
        self.assertGreater(len(expected), 0)
        for page in pages:
            self.assertTrue((ROOT / page["source"]).is_file(), page["source"])
            self.assertTrue(page["title"].strip() and page["description"].strip(), page["slug"])
            self.assertNotIn("Venom - Executor", page["title"])

    def test_a_directory_without_a_readme_is_skipped(self):
        root = scratch({"executors/http/README.md": readme("HTTP"), "executors/empty/notes.txt": "x"}, [INDEX])
        self.assertEqual([p["slug"] for p in site_executors.expand(root, GENERATOR)], ["http"])

    def test_the_internal_folder_produces_no_page(self):
        root = scratch({"executors/http/README.md": readme("HTTP"), "executors/internal/tlsconfig/x.go": "package x"}, [INDEX])
        self.assertEqual([p["slug"] for p in site_executors.expand(root, GENERATOR)], ["http"])

    def test_the_plugins_readme_is_the_plugins_page(self):
        root = scratch({"executors/plugins/README.md": "## Venom Executor\n\ntext\n"}, [INDEX])
        page = site_executors.expand(root, GENERATOR)[0]
        self.assertEqual((page["slug"], page["title"], page["source"]), ("plugins", "Plugins", "executors/plugins/README.md"))
        self.assertTrue(page["description"].strip())

    def test_the_executors_readme_is_the_writing_executors_page(self):
        root = scratch({"executors/README.md": "# Executors\n\ntext\n", "executors/http/README.md": readme("HTTP")}, [INDEX])
        pages = {p["slug"]: p for p in site_executors.expand(root, GENERATOR)}
        self.assertEqual(set(pages), {"http", "writing-executors"})
        self.assertEqual((pages["writing-executors"]["title"], pages["writing-executors"]["source"]),
                         ("Writing executors", "executors/README.md"))
        self.assertTrue(pages["writing-executors"]["description"].strip())


class ThroughTheBuild(unittest.TestCase):
    def test_a_generator_entry_builds_one_html_file_per_executor(self):
        root = scratch({"executors/http/README.md": readme("HTTP"), "executors/web/README.md": readme("Web")}, [INDEX, GENERATOR])
        out = root / "_out"
        self.assertEqual(bs.build(root, out), [])
        for slug in ("http", "web"):
            self.assertIn("<title>", (out / f"{slug}.html").read_text(encoding="utf-8"))

    def test_a_link_between_two_executor_readmes_becomes_a_site_link(self):
        root = scratch({"executors/http/README.md": readme("HTTP", "See [web](../web/README.md).\n"),
                        "executors/web/README.md": readme("Web")}, [INDEX, GENERATOR])
        out = root / "_out"
        self.assertEqual(bs.build(root, out), [])
        self.assertIn('href="web.html"', (out / "http.html").read_text(encoding="utf-8"))

    def test_a_link_to_a_missing_file_is_a_build_error_naming_the_link(self):
        root = scratch({"executors/http/README.md": readme("HTTP", "See [gone](../gone/README.md).\n")}, [INDEX, GENERATOR])
        errors = bs.build(root, root / "_out")
        self.assertEqual(len(errors), 1)
        self.assertIn("../gone/README.md", errors[0])

    def test_an_unknown_generator_is_a_build_error_naming_it(self):
        root = scratch({}, [INDEX, {"generator": "nonesuch"}])
        with self.assertRaises(bs.BuildError) as caught:
            bs.load_config(root)
        self.assertIn("nonesuch", str(caught.exception))

    def test_a_generator_entry_with_a_slug_is_a_build_error(self):
        root = scratch({"executors/http/README.md": readme("HTTP")}, [INDEX, {**GENERATOR, "slug": "x"}])
        with self.assertRaises(bs.BuildError) as caught:
            bs.load_config(root)
        self.assertIn("generator", str(caught.exception))

    def test_a_generator_entry_with_a_source_is_a_build_error(self):
        root = scratch({"executors/http/README.md": readme("HTTP")}, [INDEX, {**GENERATOR, "source": "index.md"}])
        with self.assertRaises(bs.BuildError) as caught:
            bs.load_config(root)
        self.assertIn("generator", str(caught.exception))

    def test_load_config_expands_the_generator_into_ordinary_pages(self):
        root = scratch({"executors/http/README.md": readme("HTTP")}, [INDEX, GENERATOR])
        slugs = [p["slug"] for p in bs.load_config(root)["pages"]]
        self.assertEqual(slugs, ["index", "http"])


if __name__ == "__main__":
    unittest.main()
