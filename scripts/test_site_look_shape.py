#!/usr/bin/env python3
"""Tests of the shape, components and motion of the site stylesheet (story #3667, task #3672). The contract is the
Shape, Components, Motion and Accessibility sections of site/DESIGN.md. They read the REAL site/ folder.
Offline, standard library only. Run: python scripts/test_site_look_shape.py
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_site import ROOT  # noqa: E402
from test_site_look_tokens import matching_brace, split_top, stylesheet  # noqa: E402

FOCUS_TAGS = {"a", "button", "input", "summary"}
FOCUS_CLASSES = {"btn", "cta", "copy-button", "theme-button", "menu-toggle"}
FOCUS_ELEMENTS = ("a", "button", "summary", "input", ".copy-button", ".theme-button", ".menu-toggle")
WARNING_WORDS = ("warn", "error", "fail", "danger")
RESERVED_FAIL_SELECTORS = (".n",)  # the run-output FAIL / number class that already used --fail


def spaced_rules(css: str, context: tuple[str, ...] = ()):
    """Rules as (enclosing @media preludes, selector with its spaces, declarations name -> value)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules, pos = [], 0
    while True:
        open_at = css.find("{", pos)
        if open_at < 0:
            return rules
        prelude = css[pos:open_at].strip()
        close = matching_brace(css, open_at)
        body = css[open_at + 1:close]
        if prelude.startswith("@media") or prelude.startswith("@supports"):
            rules += spaced_rules(body, context + (re.sub(r"\s+", "", prelude),))
        else:
            declarations = []
            for chunk in split_top(body, ";"):
                name, colon, value = chunk.partition(":")
                if colon and name.strip():
                    declarations.append((name.strip().lower(), normal(value)))
            for selector in split_top(prelude, ","):
                rules.append((context, re.sub(r"\s+", " ", selector.replace("'", '"')).strip(), declarations))
        pos = close + 1


def normal(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\s*!important\s*$", "", value.strip(), flags=re.I)).lower()


def compound(selector: str) -> tuple[str, list[str]]:
    """The tag and the classes of the last compound selector, pseudo-classes and attributes removed."""
    last = re.split(r"\s*[>+~]\s*|\s+", selector.strip())[-1]
    base = re.sub(r"\[[^\]]*\]", "", last)
    base = re.sub(r"::?[\w-]+(?:\([^)]*\))?", "", base)
    tag = re.match(r"[a-z][\w-]*", base)
    return (tag.group(0) if tag else ""), re.findall(r"\.([\w-]+)", base)


def targets_focusable(selector: str) -> bool:
    tag, classes = compound(selector)
    return tag in FOCUS_TAGS or bool(FOCUS_CLASSES & set(classes))


def focusable_clip_paths(css: str) -> list[str]:
    """Selectors that can take keyboard focus and set clip-path (it would clip the focus ring)."""
    return [selector for _, selector, declarations in spaced_rules(css)
            if targets_focusable(selector) and any(name == "clip-path" for name, _ in declarations)]


def clipped_ring_problems(css: str) -> list[str]:
    """Clipped containers (clip-path, not the inset(50%) visually-hidden trick) whose scrollable pre has no
    `<container> pre:focus-visible` rule with a negative outline-offset: the ring would be cut off."""
    rules = spaced_rules(css)
    containers = sorted({s for _, s, d in rules for n, v in d if n == "clip-path" and v != "inset(50%)"})
    problems = []
    for container in containers:
        pattern = rf"{re.escape(container)} (?:.* )?pre:focus-visible"
        offsets = [dict(d).get("outline-offset", "") for _, s, d in rules if re.fullmatch(pattern, s)]
        if not any(re.fullmatch(r"-[1-9]\d*px", o) for o in offsets):
            problems.append(f"{container} pre:focus-visible has no negative outline-offset: {offsets}")
    return problems


def declared(rules, name_filter):
    return [(context, selector, name, value) for context, selector, declarations in rules
            for name, value in declarations if name_filter(name)]


def own_class_rules(rules, class_name: str):
    return [(c, s, d) for c, s, d in rules if s == f".{class_name}"]


class Shape(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = spaced_rules(stylesheet())

    def test_every_border_radius_is_zero_avatars_and_pills_included(self):
        bad = [f"{s} {n}: {v}" for _, s, n, v in declared(self.rules, lambda n: "radius" in n)
               if not re.fullmatch(r"0(?:px)?(?:\s+0(?:px)?){0,3}", v)]
        self.assertEqual(bad, [])

    def test_no_box_shadow_other_than_none_anywhere_media_blocks_and_search_overlay_included(self):
        bad = [f"{s} {v}" for _, s, n, v in declared(self.rules, lambda n: n == "box-shadow") if v != "none"]
        self.assertEqual(bad, [])

    def test_no_text_shadow_and_no_drop_shadow_filter(self):
        bad = [f"{s} {n}: {v}" for _, s, n, v in declared(self.rules, lambda n: n in ("text-shadow", "filter", "backdrop-filter"))
               if n == "text-shadow" and v != "none" or "drop-shadow" in v]
        self.assertEqual(bad, [])

    def test_the_radius_checker_rejects_a_round_avatar(self):
        rules = spaced_rules(".avatar { border-radius: 50%; } .ok { border-radius: 0; }")
        bad = [s for _, s, n, v in declared(rules, lambda n: "radius" in n)
               if not re.fullmatch(r"0(?:px)?(?:\s+0(?:px)?){0,3}", v)]
        self.assertEqual(bad, [".avatar"])


class FocusAndCut(unittest.TestCase):
    def setUp(self) -> None:
        self.css = stylesheet()
        self.rules = spaced_rules(self.css)

    def focus_rules(self):
        return [(s, dict(d)) for _, s, d in self.rules if ":focus-visible" in s and "outline" in dict(d)]

    def test_the_focus_ring_is_3px_solid_venom_with_an_offset(self):
        rules = [(s, d) for s, d in self.focus_rules() if d["outline"] == "3px solid var(--venom)"]
        self.assertTrue(rules, "no :focus-visible rule with outline: 3px solid var(--venom)")
        self.assertTrue(any(re.fullmatch(r"[1-9]\d*px", d.get("outline-offset", "")) for _, d in rules))

    def test_the_focus_ring_covers_links_buttons_and_the_controls(self):
        selectors = [s for s, d in self.focus_rules() if d["outline"] == "3px solid var(--venom)"]
        universal = any(s in (":focus-visible", "*:focus-visible") for s in selectors)
        missing = [e for e in FOCUS_ELEMENTS
                   if not universal and not any(s.startswith(e + ":focus-visible") for s in selectors)]
        self.assertEqual(missing, [])

    def test_no_focusable_selector_sets_clip_path(self):
        self.assertEqual(focusable_clip_paths(self.css), [])

    def test_the_checker_reports_planted_clip_paths_on_focusable_selectors(self):
        for planted in (".cta { clip-path: polygon(0 0, 100% 0, 100% 100%); }",
                        ".prose a:hover { clip-path: inset(0); }", "button.x { clip-path: inset(0); }",
                        ".btn { clip-path: polygon(0 0); }", "summary { clip-path: inset(0); }"):
            with self.subTest(planted=planted):
                self.assertEqual(len(focusable_clip_paths(planted)), 1)

    def test_the_checker_accepts_clip_path_on_a_panel_and_on_cut(self):
        self.assertEqual(focusable_clip_paths(".cut { clip-path: polygon(0 0); } .panel { clip-path: inset(0); }"), [])

    def test_every_clipped_container_has_an_inside_focus_ring_for_its_pre(self):
        self.assertEqual(clipped_ring_problems(self.css), [])

    def test_the_clipped_container_checker_reports_a_missing_or_outward_ring(self):
        clipped = ".panel { clip-path: inset(0); }"
        self.assertEqual(len(clipped_ring_problems(clipped)), 1)
        self.assertEqual(len(clipped_ring_problems(clipped + ".panel pre:focus-visible { outline-offset: 3px; }")), 1)
        self.assertEqual(clipped_ring_problems(clipped + ".panel pre:focus-visible { outline-offset: -3px; }"), [])
        self.assertEqual(clipped_ring_problems(".s { clip-path: inset(50%); }"), [])

    def test_the_button_cut_is_a_14px_background_gradient(self):
        values = [v for _, s, d in self.rules if compound(s) == ("", ["btn"]) for n, v in d
                  if n in ("background", "background-image")]
        self.assertTrue(any("linear-gradient(225deg" in v and "14px" in v for v in values), values)

    def test_the_cut_utility_makes_a_14px_polygon_for_panels(self):
        values = [dict(d).get("clip-path", "") for _, _, d in own_class_rules(self.rules, "cut")]
        self.assertTrue(any(v.startswith("polygon(") and "14px" in v for v in values), values)


class Hooks(unittest.TestCase):
    """Selectors the page scripts and the markup rely on stay styled."""

    def setUp(self) -> None:
        self.selectors = [s for _, s, _ in spaced_rules(stylesheet())]
        self.template = (ROOT / "site" / "template.html").read_text(encoding="utf-8")

    def styled(self, pattern: str) -> bool:
        return any(re.search(pattern, s) for s in self.selectors)

    def test_the_hooks_of_the_page_scripts_are_styled(self):
        hooks = {"theme button": r"\.theme-button", "auto icon": r"\.i-auto", "light icon": r"\.i-light",
                 "dark icon": r"\.i-dark", "sidebar": r"#sidebar|\.side(?![\w-])", "phone menu toggle": r"\.menu-toggle",
                 "open menu": r"\.menu-open", "copy button": r"\.copy-button", "on-page contents": r"\.toc(?![\w-])",
                 "code block in prose": r"\.prose pre|\.prose\b.*\bpre\b", "sidebar group": r"\.side-group"}
        self.assertEqual([name for name, pattern in hooks.items() if not self.styled(pattern)], [])

    def test_the_classes_of_the_page_shell_are_styled(self):
        classes = set(re.findall(r'class="([\w -]+)"', self.template))
        names = {c for value in classes for c in value.split()}
        self.assertTrue({"layout", "side", "prose", "toc", "foot"} <= names)
        self.assertEqual([c for c in sorted(names) if not self.styled(rf"\.{re.escape(c)}(?![\w-])")], [])


class MotionAndLattice(unittest.TestCase):
    def setUp(self) -> None:
        self.css = stylesheet()
        self.rules = spaced_rules(self.css)

    def test_reduced_motion_switches_transitions_and_animations_off(self):
        found = [(n, v) for context, _, d in self.rules if "@media(prefers-reduced-motion:reduce)" in context
                 for n, v in d]
        self.assertTrue(any((n, v) in (("transition", "none"), ("animation", "none"), ("scroll-behavior", "auto"))
                            for n, v in found), found)

    def test_a_phone_width_query_of_900px_or_less_exists(self):
        widths = [int(w) for context, _, _ in self.rules for c in context
                  for w in re.findall(r"\(max-width:(\d+)px\)", c)]
        self.assertTrue(widths and min(widths) <= 900, widths)

    def test_the_lattice_property_is_an_inline_svg(self):
        values = [v for _, _, d in self.rules for n, v in d if n == "--lattice"]
        self.assertTrue(any("url(" in v and "data:image/svg+xml" in v for v in values), values)

    def test_the_lattice_is_used_in_the_hero_and_the_footer_and_nowhere_else(self):
        users = [s for _, s, d in self.rules if any("var(--lattice" in v for _, v in d)]
        self.assertTrue(any(".hero" in s for s in users), users)
        self.assertTrue(any(".foot" in s or "footer" in s for s in users), users)
        self.assertEqual([s for s in users if not re.search(r"\.hero|\.foot|footer", s)], [])


class Callouts(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = spaced_rules(stylesheet())

    def test_blockquote_has_a_venom_border_and_no_fail_colour(self):
        quote = [d for _, s, d in self.rules if compound(s) == ("blockquote", []) and s == "blockquote"]
        self.assertTrue(quote)
        borders = [v for d in quote for n, v in d if n.startswith("border")]
        self.assertTrue(any("var(--venom)" in v for v in borders), borders)
        self.assertEqual([v for d in quote for _, v in d if "var(--fail" in v], [])

    def test_fail_colour_is_used_only_for_warnings_errors_and_the_run_output_class(self):
        bad = [f"{s} {n}: {v}" for _, s, d in self.rules for n, v in d
               if "var(--fail" in v and not n.startswith("--")
               and not any(w in s for w in WARNING_WORDS) and s not in RESERVED_FAIL_SELECTORS]
        self.assertEqual(bad, [])

    def test_the_current_page_in_the_sidebar_has_a_4px_venom_bar_on_the_left(self):
        found = [dict(d) for _, s, d in self.rules if '[aria-current="page"]' in s]
        self.assertTrue(found)
        self.assertTrue(any(d.get("border-left") == "4px solid var(--venom)" for d in found), found)

    def test_table_rows_have_a_hairline_bottom_border_from_line(self):
        cells = [dict(d) for _, s, d in self.rules if compound(s)[0] in ("th", "td", "tr")]
        self.assertTrue(any(re.fullmatch(r"[12]px solid var\(--line\)", d.get("border-bottom", "")) for d in cells), cells)

    def test_table_cells_have_no_all_round_border(self):
        bad = [d["border"] for _, s, d in ((c, s, dict(d)) for c, s, d in self.rules)
               if compound(s)[0] in ("th", "td") and d.get("border", "none") not in ("none", "0")]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
