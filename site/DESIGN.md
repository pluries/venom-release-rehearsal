# Pluries Venom docs site: design

Status: approved by the maker on 2026-11-04 (story #3667), applied to the home page by task #3673. Direction: **bold and graphic**, the snake as the logo, the name Pluries Venom, and **not the previous look**.

## In one paragraph

Pluries Venom reads like a poster for a test runner: big, expanded display type, a near-black ground, one green accent that always means "this is the thing", and a repeating scale pattern that gives pages a texture without a single stock illustration. Content stays plain and fast to read; the boldness lives in the frame around it.

## Brand

- **Name**: "Pluries Venom" for the fork and the site, written in uppercase in the header (`PLURIES VENOM`). The command stays `venom`, and "ovh/venom" is only ever named as upstream.
- **Logo**: the flat snake of Microsoft's Fluent Emoji (MIT licence), as an image (`site/img/snake.svg`) set at 28 px before the wordmark, and as the favicon; `site/img/snake-180.png` (apple touch icon) and `site/img/favicon.ico` are rendered once from the same drawing (through Windows' Segoe UI Emoji font, which draws the same snake), and committed. The Fluent source is `assets/Snake/Flat/snake_flat.svg` of github.com/microsoft/fluentui-emoji at commit `1ffb34c7`; its MIT notice ships as `site/img/LICENSE-fluent-emoji.txt`.
- The page titles read "Page · Pluries Venom"; the footer says "Pluries Venom is an independent fork of ovh/venom, published under the Apache-2.0 licence" with the upstream and licence links (FR-06).

- **The green** is the body colour of the 🐍 emoji, `#00d26a`, read from the pixels as Windows renders it (Segoe UI Emoji). In the light theme the same hue is darkened to `#006c35`, because the original is only 1.8:1 on the light ground.
- **Why an image**: other systems draw the snake emoji in other greens, so the logo and the favicon are an image, not the emoji character. This keeps the logo identical everywhere.

## What changes from today

The site was started from another project's template. Everything below replaces it, so the two products no longer look related.

| Part | Today (the template the site started from) | New |
|---|---|---|
| Display font | the template's first family | Archivo, 800, expanded width |
| Body font | the template's second family | Public Sans |
| Code font | the template's mono family | IBM Plex Mono |
| Accent | deep teal-green on warm neutrals | venom green on ink (dark), moss on bone (light) |
| Shape | rounded cards, soft shadows | square blocks with one cut corner, hard 2 px rules, no shadows |
| Home | centred hero, three cards | left-heavy poster hero, a real captured run (all PASS) as the picture |
| Texture | none | a scale lattice, used at low contrast on the hero and the footer only |

## Colour

Dark is the default; light is a full design, not an inversion. All text pairs meet WCAG AA at the sizes used (ratios measured).

| Token | Dark | Light | Used for |
|---|---|---|---|
| `--ink` | `#0b0d0a` | `#f2f4ee` | page ground |
| `--ink-2` | `#151a13` | `#e4e8dd` | panels, code blocks |
| `--bone` | `#f2f4ee` | `#0b0d0a` | body text (17.6:1 on the ground) |
| `--mute` | `#a3ad9a` | `#4b5645` | secondary text (8.4:1 dark, 7.0:1 light) |
| `--venom` | `#00d26a` | `#006c35` | links, rules that mark the current place, the one call to action (9.7:1 dark, 5.9:1 light) |
| `--on-venom` | `#0b0d0a` | `#f2f4ee` | text on a filled venom block (9.7:1 dark, 5.9:1 light) |
| `--fail` | `#ff5a45` | `#b3261e` | FAIL lines and error callouts only (6.3:1, 5.9:1) |
| `--line` | `#2a3126` | `#c9cfc0` | hairlines between content blocks |

Rules: the green never fills more than one block per screen; `--fail` never decorates; no decorative gradients (the only gradient is the corner cut of the buttons).

## Type

- **Display**: Archivo 800 at width 125, uppercase for page titles and the home headline, tight leading (0.95), letter-spacing 0. The home headline is fluid, `clamp(44px, 8vw, 92px)`; page titles and headings step down from 48 to 24 px on desktop and 32 to 20 px on phones.
- **Body**: Public Sans 400 and 600 at 17 px, line height 1.6, measure 68 characters.
- **Code**: IBM Plex Mono 400 at 15 px; command and YAML blocks sit on `--ink-2` with a 4 px venom rule on the left.
- Fonts are served from the site itself (no third-party requests), as WOFF2 subsets, with their open licences next to them.

## Shape and the scale motif

- Blocks are rectangles with one 14 px diagonal cut on the top right corner, like a fang. Panels that cannot take focus (the run output, the hero panel) make the cut with `clip-path`. Anything that can take keyboard focus (buttons, links) never uses `clip-path`, because it would clip the focus ring: its cut is a gradient corner painted on the background (`linear-gradient(225deg, transparent 14px, <fill> 0)`).
- Separators are 2 px solid `--line`; the current page in the sidebar gets a 4 px venom bar.
- The scale lattice is one repeating inline SVG (diamonds, 24 px pitch, 1 px stroke at 8% opacity). It appears behind the home hero and in the footer, nowhere else.

## Layout

- Home: a 12 column grid. The headline takes columns 1 to 9 ("YAML in. PASS out."), the lead 1 to 8. Below them, in its own row, the demo: two panels side by side (columns 1 to 6 and 7 to 12), the YAML suite and a real captured venom run (all PASS, from `site/examples/run-output.txt`) on terminal-style panels; they stack on phones. Under it, one row of four short facts, then the quickstart command with a copy button.
- Docs pages: sidebar (260 px, sticky) / content (68 characters) / on-page contents (220 px, hidden below 1100 px). Header is a solid ink bar with the snake logo and the wordmark PLURIES VENOM, "Docs" and "Reference", the theme button and the GitHub link. The on-page contents (`.toc`) is styled with the same bar and rules as the sidebar.
- Footer: the fork notice, upstream and licence links, on the lattice, same words as today (FR-06).

## Components

| Component | Rule |
|---|---|
| Link | venom colour, 2 px underline offset 3 px; hover fills with venom and flips the text to `--on-venom` |
| Button | filled venom block with the fang cut made by a background gradient (no `clip-path`), uppercase 14 px Archivo; one per screen; the focus ring stays fully visible |
| Code block | `--ink-2`, left venom rule, Copy button top right, horizontal scroll inside the block |
| Run output | `PASS` lines in venom green, `FAIL` lines (class `.fail`) in `--fail`, grey prompt; a real capture without timings, never invented; used on the home page |
| Callout | 2 px rule in the callout's colour (venom for tips, `--fail` for warnings), no icon fill |
| Table | hairline rows, header in uppercase mute, scrolls inside its own container on phones |

## Motion

Almost none. A 120 ms colour change on links and buttons. The home run output is static text: the site works without JavaScript, so nothing types in. No scroll effects.

## Accessibility and content rules

- Contrast: every text pair is at least 4.5:1 (AA), measured on both surfaces, `--ink` and `--ink-2` (code blocks and panels), in both themes, plus the filled-button pair (`--on-venom` on `--venom`); keyboard focus is a 3 px venom outline with offset on both themes and is never clipped.
- Facts shown on the page are computed or tested, never typed in by hand: the executor count on the home page equals the number of executor pages.
- The site works without JavaScript except copy buttons, the theme button and search.
- No cookies, no analytics, no third-party requests; images and fonts come from the site (FR-07).
- Voice: short sentences, commands in code blocks, no marketing adjectives, the word "venom" for the tool and "the fork" for this project.
- Never use the previous look's fonts, its teal accent, rounded shadowed cards or its home layout.

## Implementation notes

- The tokens live in `site/style.css` as custom properties (four theme blocks); `site/template.html`, `site/home.html` and the fonts folder carry the rest; `site/pages/` and the page content did not change.
- The checks on the look are `scripts/test_site_look_tokens.py` (colours, contrast), `_fonts.py`, `_logo.py`, `_shape.py` (shape, focus, hooks, motion) and `_home.py` (home page, footer, titles); each was written before the CSS or markup it covers.
- The fonts (SIL OFL) and the snake (MIT) ship with their licence files in `site/fonts/` and `site/img/`.
- The nginx policy of the site image must allow what the CSS uses: images from the site and `data:` images (the lattice).
