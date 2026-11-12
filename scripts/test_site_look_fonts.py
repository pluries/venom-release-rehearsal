#!/usr/bin/env python3
"""Tests of the self-hosted fonts of the site (story #3667, task #3670). The contract is the Type section of site/DESIGN.md.
They read the REAL site/ folder and a scratch build of it. Offline, standard library only. Run: python scripts/test_site_look_fonts.py
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_site_look as look  # noqa: E402
from test_build_site import ROOT  # noqa: E402
from test_site_look_tokens import DARK_ROOT, parse, squash, stylesheet  # noqa: E402

FAMILIES = ("Archivo", "Public Sans", "IBM Plex Mono")
FONT_FILES = ("archivo-latin-wdth-normal.woff2", "public-sans-latin-wght-normal.woff2",
              "ibm-plex-mono-latin-400-normal.woff2", "ibm-plex-mono-latin-500-normal.woff2")
# family: (weight, stretch or None, file) per expected face
FACES = {
    "Archivo": [("100 900", "62% 125%", "archivo-latin-wdth-normal.woff2")],
    "Public Sans": [("100 900", None, "public-sans-latin-wght-normal.woff2")],
    "IBM Plex Mono": [("400", None, "ibm-plex-mono-latin-400-normal.woff2"),
                      ("500", None, "ibm-plex-mono-latin-500-normal.woff2")],
}
PACKAGES = {"Archivo": "@fontsource-variable/archivo@5.1.1", "Public Sans": "@fontsource-variable/public-sans@5.1.1",
            "IBM Plex Mono": "@fontsource/ibm-plex-mono@5.1.1"}
OLD_FAMILIES = ("bricolage grotesque", "instrument sans", "jetbrains mono")
GENERIC = {"serif", "sans-serif", "monospace", "system-ui", "ui-monospace", "ui-sans-serif"}
SRC_URL = re.compile(r"^url\(\s*([\"']?)([^\"')]+)\1\s*\)\s*format\(\s*[\"']woff2[\"']\s*\)$", re.I)


def font_faces(rules) -> list[dict[str, str]]:
    return [{k.lower(): v for k, v in declarations} for context, selector, declarations in rules
            if selector == "@font-face" and not context]


def unquote(value: str) -> str:
    return value.strip().strip("\"'")


def families_of(value: str, root: dict[str, str]) -> list[str]:
    """The font-family list of a declaration, a var(--x) resolved from the bare :root."""
    match = re.fullmatch(r"var\(\s*(--[\w-]+)\s*(?:,[^)]*)?\)", value.strip())
    if match:
        value = root.get(match.group(1), "")
    return [unquote(part) for part in value.split(",") if part.strip()]


def licence_sections(text: str) -> dict[str, str]:
    parts = re.split(r"^## +(.+?)\s*$", text, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def licence_problems(text: str, families=FAMILIES) -> list[str]:
    """One message per family without its own section naming the SIL Open Font License, a copyright line and its package."""
    sections, found = licence_sections(text), []
    for family in families:
        body = sections.get(family)
        if body is None:
            found.append(f"{family}: no section")
            continue
        if not re.search(r"SIL Open Font License", body, re.I):
            found.append(f"{family}: no SIL Open Font License")
        if not re.search(r"copyright", body, re.I):
            found.append(f"{family}: no copyright line")
        if PACKAGES.get(family) and PACKAGES[family] not in body:
            found.append(f"{family}: no source {PACKAGES[family]}")
    return found


class FontFaces(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = parse(stylesheet())
        self.faces = font_faces(self.rules)

    def by_family(self) -> dict[str, list[dict[str, str]]]:
        grouped: dict[str, list[dict[str, str]]] = {}
        for face in self.faces:
            grouped.setdefault(unquote(face.get("font-family", "")), []).append(face)
        return grouped

    def test_exactly_the_three_families_are_declared(self):
        self.assertEqual(sorted(self.by_family()), sorted(FAMILIES))

    def test_each_family_has_the_expected_number_of_faces(self):
        grouped = self.by_family()
        for family, expected in FACES.items():
            with self.subTest(family=family):
                self.assertEqual(len(grouped.get(family, [])), len(expected))

    def test_the_weights_and_stretch_ranges_match_the_design(self):
        grouped = self.by_family()
        for family, expected in FACES.items():
            actual = sorted((f.get("font-weight"), f.get("font-stretch")) for f in grouped.get(family, []))
            with self.subTest(family=family):
                self.assertEqual(actual, sorted((w, s) for w, s, _ in expected))

    def test_each_src_is_a_relative_woff2_url_of_the_expected_file(self):
        grouped = self.by_family()
        for family, expected in FACES.items():
            actual = []
            for face in grouped.get(family, []):
                match = SRC_URL.match(face.get("src", "").strip())
                actual.append(match.group(2) if match else f"unparsed: {face.get('src')}")
            with self.subTest(family=family):
                self.assertEqual(sorted(actual), sorted(f"fonts/{name}" for _, _, name in expected))

    def test_no_import_and_no_remote_address_in_the_stylesheet(self):
        css = stylesheet()
        self.assertNotIn("@import", css.lower())
        for match in look.CSS_URL.finditer(css):
            with self.subTest(address=match.group(2)):
                self.assertFalse(look.css_address_is_remote(match.group(2)))
                self.assertFalse(match.group(2).startswith("//"))

    def test_every_face_swaps_while_loading(self):
        self.assertTrue(self.faces)
        for face in self.faces:
            with self.subTest(family=face.get("font-family")):
                self.assertEqual(face.get("font-display"), "swap")

    def test_every_declared_file_exists_and_is_woff2(self):
        for _, _, name in (face for faces in FACES.values() for face in faces):
            path = ROOT / "site" / "fonts" / name
            with self.subTest(file=name):
                self.assertTrue(path.is_file(), "no such file")
                self.assertEqual(path.read_bytes()[:4], b"wOF2")


class FamilyDeclarations(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = parse(stylesheet())
        self.root = {k: v for _, sel, decls in self.rules if sel == DARK_ROOT for k, v in decls if k.startswith("--")}

    def declared(self, selector_matches) -> list[tuple[str, list[str]]]:
        return [(sel, families_of(value, self.root)) for ctx, sel, decls in self.rules
                if sel != "@font-face" and selector_matches(sel) for name, value in decls if name == "font-family"]

    def test_the_body_uses_public_sans_first(self):
        found = self.declared(lambda s: s == "body")
        self.assertTrue(found, "no font-family on body")
        for _, families in found:
            self.assertEqual(families[:1], ["Public Sans"])

    def test_the_headings_use_archivo_first(self):
        found = self.declared(lambda s: re.search(r"\bh[1-3]\b", s) is not None)
        self.assertTrue(found, "no font-family on headings")
        for selector, families in found:
            with self.subTest(selector=selector):
                self.assertEqual(families[:1], ["Archivo"])

    def test_code_and_pre_use_ibm_plex_mono_first(self):
        for tag in ("code", "pre"):
            found = self.declared(lambda s, tag=tag: s == tag)
            with self.subTest(tag=tag):
                self.assertTrue(found, f"no font-family on {tag}")
                for _, families in found:
                    self.assertEqual(families[:1], ["IBM Plex Mono"])

    def test_every_font_family_declaration_names_one_of_the_three_first_with_a_generic_fallback(self):
        found = self.declared(lambda s: True)
        self.assertTrue(found)
        for selector, families in found:
            with self.subTest(selector=selector):
                self.assertIn(families[0] if families else "", FAMILIES)
                self.assertIn(families[-1].lower() if families else "", GENERIC)


class OldFontsGone(unittest.TestCase):
    def texts(self) -> dict[str, str]:
        return {str(p.relative_to(ROOT)): p.read_text(encoding="utf-8", errors="replace")
                for p in sorted((ROOT / "site").rglob("*")) if p.is_file() and p.suffix in (".html", ".css", ".js")}

    def old_names_in(self, text: str) -> list[str]:
        flat = re.sub(r"[-_\s]+", " ", text.lower())
        return [name for name in OLD_FAMILIES if name in flat]

    def test_the_stylesheet_and_every_html_css_js_file_name_no_old_family_or_file(self):
        texts = self.texts()
        self.assertIn("site/style.css", {k.replace("\\", "/") for k in texts})
        for name, text in texts.items():
            with self.subTest(file=name):
                self.assertEqual(self.old_names_in(text), [])

    def test_the_old_font_files_are_not_in_the_site_folder(self):
        stale = [p.name for p in (ROOT / "site" / "fonts").glob("*.woff2") if self.old_names_in(p.name)]
        self.assertEqual(stale, [])

    def test_the_detector_finds_planted_old_names(self):
        for text in ('font-family: "Bricolage Grotesque"', "fonts/instrument-sans-latin-400-normal.woff2", "JETBRAINS MONO"):
            with self.subTest(text=text):
                self.assertTrue(self.old_names_in(text))
        self.assertEqual(self.old_names_in('font-family: "Archivo", sans-serif'), [])


GOOD_LICENCES = "\n".join(
    f"# Font licences\n\n## {family}\n\nCopyright 2020 The {family} Project Authors. Source: {PACKAGES[family]}.\n"
    "Licensed under the SIL Open Font License, Version 1.1.\n" for family in FAMILIES)


class Licences(unittest.TestCase):
    def test_the_published_licence_file_has_a_complete_section_per_family(self):
        path = ROOT / "site" / "fonts" / "LICENSES.md"
        self.assertTrue(path.is_file())
        self.assertEqual(licence_problems(path.read_text(encoding="utf-8")), [])

    def test_the_checker_accepts_a_complete_text(self):
        self.assertEqual(licence_problems(GOOD_LICENCES), [])

    def test_the_checker_reports_a_family_without_a_section(self):
        text = GOOD_LICENCES.replace("## IBM Plex Mono", "## Something Else")
        found = licence_problems(text)
        self.assertEqual(len(found), 1)
        self.assertIn("IBM Plex Mono", found[0])

    def test_the_checker_reports_a_section_without_the_licence_name(self):
        text = GOOD_LICENCES.replace("SIL Open Font License", "some licence", 1)
        found = licence_problems(text)
        self.assertEqual(len(found), 1)
        self.assertIn("Archivo", found[0])


class BuiltSite(look.LookCase):
    def test_no_css_url_or_import_in_the_built_site_points_to_another_host(self):
        out = self.standard()
        sheets = sorted(out.rglob("*.css"))
        self.assertTrue(sheets)
        for sheet in sheets:
            css = sheet.read_text(encoding="utf-8")
            addresses = [m.group(2) for m in look.CSS_URL.finditer(css)] + look.CSS_IMPORT.findall(css)
            for address in addresses:
                with self.subTest(file=sheet.name, address=address):
                    self.assertFalse(look.css_address_is_remote(address))

    def test_the_built_fonts_folder_holds_exactly_the_four_woff2_files_and_the_licences(self):
        fonts = self.standard() / "fonts"
        self.assertEqual(sorted(p.name for p in fonts.iterdir()), sorted(FONT_FILES + ("LICENSES.md",)))

    def test_the_built_site_holds_no_other_font_file(self):
        out = self.standard()
        found = sorted(p.relative_to(out).as_posix() for p in out.rglob("*")
                       if p.suffix.lower() in (".woff2", ".woff", ".ttf", ".otf", ".eot"))
        self.assertEqual(found, sorted(f"fonts/{n}" for n in FONT_FILES))


if __name__ == "__main__":
    unittest.main()
