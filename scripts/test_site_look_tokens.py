#!/usr/bin/env python3
"""Tests of the colour tokens of the site stylesheet (story #3667, task #3669). The contract is the Colour table of site/DESIGN.md.
They read the REAL site/style.css. Offline, standard library only. Run: python scripts/test_site_look_tokens.py
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_build_site import ROOT  # noqa: E402

DARK_ROOT = ":root"
LIGHT_AUTO = ':root:not([data-theme="dark"])'
LIGHT_FORCED = ':root[data-theme="light"]'
DARK_FORCED = ':root[data-theme="dark"]'
LIGHT_MEDIA = "@media(prefers-color-scheme:light)"
TOKEN_SELECTORS = (DARK_ROOT, LIGHT_AUTO, LIGHT_FORCED, DARK_FORCED)

# token: (dark, light) from the Colour table of site/DESIGN.md
TOKENS = {
    "ink": ("#0b0d0a", "#f2f4ee"),
    "ink-2": ("#151a13", "#e4e8dd"),
    "bone": ("#f2f4ee", "#0b0d0a"),
    "mute": ("#a3ad9a", "#4b5645"),
    "venom": ("#00d26a", "#006c35"),
    "on-venom": ("#0b0d0a", "#f2f4ee"),
    "fail": ("#ff5a45", "#b3261e"),
    "line": ("#2a3126", "#c9cfc0"),
}
TEXT_PAIRS = (("bone", "ink"), ("mute", "ink"), ("venom", "ink"), ("fail", "ink"),
              ("bone", "ink-2"), ("mute", "ink-2"), ("venom", "ink-2"), ("fail", "ink-2"),
              ("on-venom", "venom"))
OLD_TOKENS = ("bg", "surface", "text", "muted", "faint", "accent", "accent-ink", "line-strong", "code", "link", "sand",
              "sage", "amber", "prose", "lead", "soft", "code-ink", "note-bg", "note-line", "note-ink", "panel-bg",
              "glow", "glow-0", "stripe", "link-under", "scrim")
NAMED = {"black", "white", "red", "green", "blue", "yellow", "orange", "purple", "pink", "gray", "grey", "silver",
         "maroon", "olive", "lime", "aqua", "teal", "navy", "fuchsia", "brown", "gold", "tan", "ivory", "beige",
         "coral", "salmon", "crimson", "cyan", "magenta", "indigo", "violet", "khaki", "lavender", "plum", "tomato",
         "orchid", "azure", "snow", "linen", "wheat", "sienna", "peru", "bisque", "chocolate", "firebrick"}
LITERAL = re.compile(r"#[0-9a-f]{3,8}\b|\b(?:rgb|rgba|hsl|hsla|hwb|lab|lch|oklab|oklch)\(", re.I)


def split_top(text: str, sep: str) -> list[str]:
    """Split on `sep` outside quotes and parentheses (a data URI holds ';' and braces)."""
    parts, depth, quote, start = [], 0, "", 0
    for i, ch in enumerate(text):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return parts


def matching_brace(text: str, open_at: int) -> int:
    depth, paren, quote = 0, 0, ""
    for i in range(open_at, len(text)):
        ch = text[i]
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif paren == 0 and ch == "{":
            depth += 1
        elif paren == 0 and ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return len(text)


def squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def parse(css: str, context: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str, list[tuple[str, str]]]]:
    """Rules as (enclosing @media preludes, selector, declarations); at-rule contexts are whitespace-free."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules, pos = [], 0
    while True:
        open_at = -1
        depth, quote = 0, ""
        for i in range(pos, len(css)):
            ch = css[i]
            if quote:
                quote = "" if ch == quote else quote
            elif ch in "\"'":
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            elif ch == "{" and depth == 0:
                open_at = i
                break
        if open_at < 0:
            return rules
        prelude = css[pos:open_at].strip()
        close = matching_brace(css, open_at)
        body = css[open_at + 1:close]
        if prelude.startswith("@media") or prelude.startswith("@supports"):
            rules += parse(body, context + (squash(prelude),))
        else:
            declarations = []
            for chunk in split_top(body, ";"):
                name, colon, value = chunk.partition(":")
                if colon and name.strip():
                    declarations.append((name.strip(), value.strip()))
            for selector in split_top(prelude, ","):
                rules.append((context, squash(selector), declarations))
        pos = close + 1


def stylesheet() -> str:
    return (ROOT / "site" / "style.css").read_text(encoding="utf-8")


def block(rules, context: tuple[str, ...], selector: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for rule_context, rule_selector, declarations in rules:
        if rule_context == context and rule_selector == squash(selector):
            values.update({k: v.lower() for k, v in declarations if k.startswith("--")})
    return values


def rgb(value: str) -> tuple[int, int, int]:
    if not re.fullmatch(r"#[0-9a-f]{6}", value):
        raise AssertionError(f"{value!r} is not a #rrggbb colour")
    return int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16)


def luminance(colour: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    red, green, blue = (channel(v) for v in colour)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast(first, second) -> float:
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def low_contrast_pairs(pairs, minimum: float = 4.5) -> list[str]:
    """pairs: (label, foreground #rrggbb, background #rrggbb); returns a message for each pair below `minimum`."""
    found = []
    for label, foreground, background in pairs:
        ratio = contrast(rgb(foreground), rgb(background))
        if ratio < minimum:
            found.append(f"{label}: {foreground} on {background} is {ratio:.2f}:1")
    return found


def themes(rules) -> dict[str, dict[str, str]]:
    """The four ways a visitor can see the site, each the dark base overridden by its own block."""
    base = block(rules, (), DARK_ROOT)
    return {
        "dark (bare :root)": base,
        "dark (data-theme)": {**base, **block(rules, (), DARK_FORCED)},
        "light (data-theme)": {**base, **block(rules, (), LIGHT_FORCED)},
        "light (automatic)": {**base, **block(rules, (LIGHT_MEDIA,), LIGHT_AUTO)},
    }


class Tokens(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = parse(stylesheet())

    def test_the_eight_tokens_are_defined_on_the_bare_root(self):
        root = block(self.rules, (), DARK_ROOT)
        for name in TOKENS:
            with self.subTest(token=name):
                self.assertIn(f"--{name}", root)

    def test_the_dark_values_match_the_design_table(self):
        root = block(self.rules, (), DARK_ROOT)
        for name, (dark, _) in TOKENS.items():
            with self.subTest(token=name):
                self.assertEqual(root.get(f"--{name}"), dark)

    def test_the_light_values_match_the_design_table_in_both_light_blocks(self):
        blocks = {"data-theme light": block(self.rules, (), LIGHT_FORCED),
                  "automatic light": block(self.rules, (LIGHT_MEDIA,), LIGHT_AUTO)}
        for where, values in blocks.items():
            for name, (_, light) in TOKENS.items():
                with self.subTest(block=where, token=name):
                    self.assertEqual(values.get(f"--{name}"), light)

    def test_venom_is_00d26a_on_the_dark_root_and_006c35_in_the_light_blocks(self):
        self.assertEqual(block(self.rules, (), DARK_ROOT).get("--venom"), "#00d26a")
        self.assertEqual(block(self.rules, (), LIGHT_FORCED).get("--venom"), "#006c35")
        self.assertEqual(block(self.rules, (LIGHT_MEDIA,), LIGHT_AUTO).get("--venom"), "#006c35")


class Contrast(unittest.TestCase):
    def setUp(self) -> None:
        self.themes = themes(parse(stylesheet()))

    def test_every_text_pair_reaches_4_5_to_1_in_every_theme(self):
        for theme, values in self.themes.items():
            pairs = [(f"{theme}: {a} on {b}", values.get(f"--{a}", "missing"), values.get(f"--{b}", "missing"))
                     for a, b in TEXT_PAIRS]
            with self.subTest(theme=theme):
                self.assertEqual(low_contrast_pairs(pairs), [])

    def test_the_checker_reports_a_planted_pair_below_4_5(self):
        found = low_contrast_pairs([("planted", "#777777", "#888888")])
        self.assertEqual(len(found), 1)
        self.assertIn("planted", found[0])

    def test_the_checker_accepts_a_pair_above_4_5(self):
        self.assertEqual(low_contrast_pairs([("fine", "#000000", "#ffffff")]), [])

    def test_the_contrast_formula_matches_the_wcag_reference_values(self):
        self.assertAlmostEqual(contrast((0, 0, 0), (255, 255, 255)), 21.0, places=2)
        self.assertAlmostEqual(contrast((119, 119, 119), (255, 255, 255)), 4.48, places=2)


class Structure(unittest.TestCase):
    def setUp(self) -> None:
        self.rules = parse(stylesheet())

    def test_the_dark_values_sit_on_the_bare_root(self):
        root = block(self.rules, (), DARK_ROOT)
        self.assertEqual({n: root.get(f"--{n}") for n in TOKENS}, {n: d for n, (d, _) in TOKENS.items()})

    def test_the_light_values_are_under_the_light_media_query_guarded_against_forced_dark(self):
        values = block(self.rules, (LIGHT_MEDIA,), LIGHT_AUTO)
        self.assertEqual({n: values.get(f"--{n}") for n in TOKENS}, {n: l for n, (_, l) in TOKENS.items()})

    def test_the_forced_light_block_carries_the_light_values(self):
        values = block(self.rules, (), LIGHT_FORCED)
        self.assertEqual({n: values.get(f"--{n}") for n in TOKENS}, {n: l for n, (_, l) in TOKENS.items()})

    def test_the_forced_dark_block_redefines_the_dark_values(self):
        values = block(self.rules, (), DARK_FORCED)
        self.assertEqual({n: values.get(f"--{n}") for n in TOKENS}, {n: d for n, (d, _) in TOKENS.items()})


def literal_colours(value: str) -> list[str]:
    cleaned = re.sub(r"url\((?:[^()]|\([^()]*\))*\)", "", value)
    cleaned = re.sub(r"\"[^\"]*\"|'[^']*'", "", cleaned)
    cleaned = re.sub(r"var\([^)]*\)", "", cleaned)
    found = LITERAL.findall(cleaned)
    found += [w for w in re.findall(r"[a-z]+", cleaned.lower()) if w in NAMED]
    return found


class NoLiteralColours(unittest.TestCase):
    def setUp(self) -> None:
        self.css = stylesheet()
        self.rules = parse(self.css)

    def test_no_colour_literal_outside_the_token_blocks_and_the_lattice_properties(self):
        offenders = []
        for context, selector, declarations in self.rules:
            if selector in {squash(s) for s in TOKEN_SELECTORS}:
                continue
            for name, value in declarations:
                if name.startswith("--") and "data:" in value:
                    continue
                if name in ("font-family", "src", "content"):
                    continue
                offenders += [f"{selector} {name}: {hit}" for hit in literal_colours(value)]
        self.assertEqual(offenders, [])

    def test_the_planted_literals_are_detected(self):
        for value in ("#fff", "1px solid #00d26a", "rgba(0, 0, 0, 0.5)", "hsl(120 50% 50%)", "red"):
            with self.subTest(value=value):
                self.assertTrue(literal_colours(value))
        for value in ("var(--venom)", "transparent", "currentColor", "none", "3px solid var(--venom)"):
            with self.subTest(value=value):
                self.assertEqual(literal_colours(value), [])

    def test_components_use_the_tokens(self):
        used = set(re.findall(r"var\(\s*--([\w-]+)", self.css))
        self.assertTrue(set(TOKENS) <= used, sorted(set(TOKENS) - used))

    def test_the_old_look_token_names_appear_nowhere(self):
        found = [n for n in OLD_TOKENS if re.search(rf"--{re.escape(n)}(?![\w-])", self.css)]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
