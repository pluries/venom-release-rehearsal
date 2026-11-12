#!/usr/bin/env python3
"""Tests of the logo and favicons of the site: the Fluent snake (story #3667, task #3671). The contract is the Brand section of site/DESIGN.md.
They read the REAL site/ folder and a scratch build of it. Offline, standard library only. Run: python scripts/test_site_look_logo.py
"""
import re
import shutil
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_site_look as look  # noqa: E402
from test_build_site import ROOT  # noqa: E402

IMG = ROOT / "site" / "img"
LICENCE = "LICENSE-fluent-emoji.txt"
FILES = ("snake.svg", "snake-180.png", "favicon.ico", LICENCE)
ICON_LINKS = (
    {"rel": "icon", "type": "image/svg+xml", "href": "img/snake.svg"},
    {"rel": "icon", "href": "img/favicon.ico", "sizes": "any"},
    {"rel": "apple-touch-icon", "href": "img/snake-180.png"},
)
FORBIDDEN_NAMES = ("favicon_png", "favicon_ico", "FAVICON_SVG", "FAVICON_ORANGE", "FAVICON_DARK", "FAVICON_ARMS", "FAVICON_STROKE",
                   "LOGO", "_in_mark", "_segment_distance", "_pixel", "_png_chunk", "PNG_SIGNATURE")
ROOT_ICONS = ("favicon.svg", "favicon.ico", "apple-touch-icon.png")


def licence_problems(folder: Path) -> list[str]:
    """What is wrong with the notice of the Fluent Emoji licence in an image folder (empty when it is there and is the MIT notice)."""
    notice = folder / LICENCE
    if not notice.is_file():
        return [f"{LICENCE} is missing"]
    text = notice.read_text(encoding="utf-8")
    return [f"{LICENCE} lacks {needle!r}" for needle in ("MIT License", "Copyright (c) Microsoft Corporation") if needle not in text]


class Head(HTMLParser):
    """The <link> tags of <head> and, of the brand link in the header, its tags and its text."""

    def __init__(self) -> None:
        super().__init__()
        self.in_head, self.in_brand = False, False
        self.links: list[dict[str, str | None]] = []
        self.brand_imgs: list[dict[str, str | None]] = []
        self.brand_svgs, self.brand_text = 0, []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "head":
            self.in_head = True
        elif tag == "link" and self.in_head:
            self.links.append(values)
        elif tag == "a" and "brand" in (values.get("class") or "").split():
            self.in_brand = True
        elif self.in_brand and tag == "img":
            self.brand_imgs.append(values)
        elif self.in_brand and tag == "svg":
            self.brand_svgs += 1

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False
        elif tag == "a":
            self.in_brand = False

    def handle_data(self, data):
        if self.in_brand:
            self.brand_text.append(data)


def head_of(text: str) -> Head:
    parser = Head()
    parser.feed(text)
    return parser


class SourceFiles(unittest.TestCase):
    def need(self, name: str) -> Path:
        path = IMG / name
        if not path.is_file():
            self.fail(f"site/img/{name} does not exist")
        return path

    def test_the_snake_is_a_plain_svg_with_the_green_fill_and_nothing_remote_or_scripted(self):
        text = self.need("snake.svg").read_text(encoding="utf-8")
        tree = ET.fromstring(text)
        self.assertIn("#00d26a", text.lower())
        tags = {element.tag.rpartition("}")[2].lower() for element in tree.iter()}
        self.assertNotIn("script", tags)
        for element in tree.iter():
            for name, value in element.attrib.items():
                if name.rpartition("}")[2].lower() == "href":
                    self.assertFalse(re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)", value.strip()), f"{name}={value}")

    def test_the_apple_touch_png_is_180_by_180(self):
        data = self.need("snake-180.png").read_bytes()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(data[12:16], b"IHDR")
        self.assertEqual(struct.unpack(">II", data[16:24]), (180, 180))

    def test_the_ico_holds_three_images_of_16_32_and_48_pixels(self):
        data = self.need("favicon.ico").read_bytes()
        reserved, kind, count = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, kind, count), (0, 1, 3))
        sizes = [(data[6 + 16 * i] or 256, data[7 + 16 * i] or 256) for i in range(count)]
        self.assertEqual(sorted(sizes), [(16, 16), (32, 32), (48, 48)])

    def test_the_folder_holds_exactly_the_four_files_and_no_other_logo(self):
        self.assertTrue(IMG.is_dir(), "site/img does not exist")
        self.assertEqual(sorted(p.name for p in IMG.iterdir()), sorted(FILES))

    def test_the_licence_notice_of_the_source_folder_is_the_mit_notice(self):
        self.assertTrue(IMG.is_dir(), "site/img does not exist")
        self.assertEqual(licence_problems(IMG), [])


class BuiltPages(look.LookCase):
    def test_every_head_carries_the_three_icon_links(self):
        for name, text in self.html_pages().items():
            links = head_of(text).links
            for expected in ICON_LINKS:
                with self.subTest(page=name, href=expected["href"]):
                    self.assertTrue(any(all(link.get(k) == v for k, v in expected.items()) for link in links), links)

    def test_the_brand_link_of_every_page_holds_the_snake_image_and_the_name(self):
        for name, text in self.html_pages().items():
            head = head_of(text)
            with self.subTest(page=name):
                self.assertEqual(head.brand_svgs, 0, "no inline svg logo")
                self.assertEqual(len(head.brand_imgs), 1)
                image = head.brand_imgs[0]
                self.assertEqual(image.get("src"), "img/snake.svg")
                self.assertEqual(image.get("alt"), "")
                self.assertTrue((image.get("width") or "").isdigit() and (image.get("height") or "").isdigit())
                self.assertIn("PLURIES VENOM", re.sub(r"\s+", " ", "".join(head.brand_text)))


class Generated(look.LookCase):
    def test_the_build_script_no_longer_draws_or_names_a_favicon_or_the_old_logo(self):
        source = (ROOT / "scripts" / "build_site.py").read_text(encoding="utf-8")
        for name in FORBIDDEN_NAMES:
            with self.subTest(name=name):
                self.assertIsNone(re.search(rf"\b{re.escape(name)}\b", source))

    def test_no_root_level_icon_is_generated(self):
        out = self.standard()
        for name in ROOT_ICONS:
            with self.subTest(name=name):
                self.assertFalse((out / name).exists())


class Published(look.LookCase):
    def test_img_holds_the_four_files_byte_identical_to_the_sources_licence_included(self):
        out = self.standard() / "img"
        self.assertTrue(IMG.is_dir(), "site/img does not exist")
        self.assertEqual(sorted(p.name for p in out.iterdir()), sorted(FILES))
        for name in FILES:
            with self.subTest(name=name):
                self.assertEqual((out / name).read_bytes(), (IMG / name).read_bytes())

    def test_a_folder_without_the_licence_is_reported_by_the_checker(self):
        self.assertTrue(IMG.is_dir(), "site/img does not exist")
        copy = Path(tempfile.mkdtemp()) / "img"
        shutil.copytree(IMG, copy)
        self.assertEqual(licence_problems(copy), [])
        (copy / LICENCE).unlink()
        self.assertEqual(licence_problems(copy), [f"{LICENCE} is missing"])


if __name__ == "__main__":
    unittest.main()
