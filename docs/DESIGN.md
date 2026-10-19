# Design system: autosmith

autosmith.eu: *STL, made by AI.* The name pairs the forge (a smith) with automation; the mark is a stack of
chevrons, a part rising layer by layer, in an Electric Blue to Teal gradient.

The web UI is a workbench for parts that Claude builds and you print. It should read like a
measuring tool: calm, exact, and quiet enough that the model on screen is the loudest thing.

Everything below lives in [`web/style.css`](../web/style.css) (tokens and components) and
[`web/icons.svg`](../web/icons.svg) (icons). A live reference with every colour, text style, font, icon and
component is in the admin section at **`/admin/design`** (next to the print-check settings; it needs a
login like the rest of admin). Change a value in `style.css`, never in a page.

## Principles

1. **The model is the hero.** Chrome stays neutral grey; colour is reserved for state
   (selected, changed, comment, fault) and for the parts themselves.
2. **Facts go in a title block.** Size, revision, dates and ids are shown in the cell grid from the
   corner of an engineering drawing (`.tb`). It is the one signature element. Use it only for facts
   about a model, never as decoration.
3. **Say what happens.** Buttons name their action (*Rebuild*, *Restore this version*,
   *Delete model*). Errors say what went wrong and what to do next. Sentence case everywhere;
   no all-caps labels, no emoji in the interface.
4. **Numbers line up.** All text uses tabular figures, so dimensions and revisions align in lists.
5. **Motion answers the user.** Hover, press and toasts move; nothing animates on its own except
   the brief highlight on a card that Claude just changed. `prefers-reduced-motion` turns it off.

## Colour

Named after the workshop materials they come from. Every colour has a light and a dark value; the
dark set applies automatically (`prefers-color-scheme`) or with `<html data-theme="dark">`.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--aluminium` | `#f8fafc` | `#0b1220` | Page background |
| `--sheet` | `#ffffff` | `#0f172a` | Panels, cards, header, dialogs |
| `--inset` | `#f1f5f9` | `#172033` | Inputs, code, hovered rows |
| `--graphite` | `#0f172a` | `#f1f5f9` | Text |
| `--scribe` | `#64748b` | `#94a3b8` | Secondary text, title block labels |
| `--rule` | `#e2e8f0` | `#1e293b` | Borders and title block rules |
| `--rule-strong` | `#cbd5e1` | `#334155` | Hovered borders, inactive dots |
| `--layout` | `#2563eb` | `#60a5fa` | Accent: primary action, selection, focus. Electric Blue (`#3b82f6`, `--brand`) darkened for text contrast |
| `--layout-wash` | `#e0ebff` | `#172554` | Selected rows, active toggles, banners |
| `--ember` | `#06b6d4` | `#22d3ee` | Brand Teal: logo gradient end, small accents. Never used for UI state |
| `--pei` | `#e2ad45` | `#e8b552` | Comments and pins. The gold of a PEI build plate |
| `--fault` | `#b8322a` | `#f0735f` | Print-check warnings, destructive actions, errors |
| `--sound` | `#2a7d4f` | `#52c486` | Live connection, current revision |
| `--stage` | `#eef2f7` | `#0d1424` | 3D viewport background (read by `viewer.html`) |
| `--stage-grid`, `--stage-grid-major` | `#dde4ec`, `#c3ccd8` | `#18233a`, `#2a3a58` | Build plate grid in the viewport |
| `--plate` | `#f6f7f9` | same | Behind preview PNGs; matches `render.BG` |

Rules of use:

- `--layout` is for one thing per view: the primary action or the current selection. Two blue
  buttons side by side is a bug.
- `--pei` always means *a comment*. Do not reuse it for warnings.
- `--fault` always means *something is wrong or will be destroyed*.
- Text contrast: `--graphite` on `--sheet` and `--scribe` on `--sheet` both pass WCAG AA in both themes.

### Part colours

Parts are drawn in a fixed order: blue `#4f8cff`, orange `#ff9a3c`, green `#5ac878`,
pink `#e65a78`, violet `#aa78ff`, cyan `#3cc8d2`. The same list is `PART_COLORS` in
[`web/common.js`](../web/common.js) and in [`render.py`](../render.py), so the preview Claude sees and
the 3D view you see always agree. Change both together.

## Type

| Role | Face | Notes |
|---|---|---|
| Interface | **Plus Jakarta Sans** (variable, weight 200–800) | Self-hosted in `web/fonts/`, OFL |
| Code, ids, parameters | **IBM Plex Mono** 400 / 500 | Only for things you would copy into code |

The brand typeface is Plus Jakarta Sans; headings use weight 700 with slightly tight tracking.

Scale (px), from the classical 11 / 12 / 14 / 16 / 21 / 28 steps:

| Token | Size | Use |
|---|---|---|
| `--t-xs` | 11 | Title block labels, badges |
| `--t-sm` | 12 | Hints, dimensions, code |
| `--t-base` | 14 | Body, buttons, inputs |
| `--t-md` | 16 | Wordmark, group headings |
| `--t-lg` | 21 | Model name, login heading |
| `--t-xl` | 28 | Page title (settings) |

Weights: 400 body, 500 buttons and labels, 600 values, 650 headings, 700 wordmark and badges.

### Text styles

Named roles, defined as `.ts-*` classes in `style.css`. Components set the same values themselves;
use a class directly only when the markup has no component of its own.

| Class | Size / weight / width | Use |
|---|---|---|
| `.ts-page` | 28 / 650 / 96% | Page titles |
| `.ts-title` | 21 / 650 / 96% | Model name, login heading |
| `.ts-heading` | 16 / 650 | Groups in settings and dialogs |
| `.ts-section` | 14 / 650 | Side panel sections |
| `.ts-body` | 14 / 400 | Running text, buttons, inputs |
| `.ts-hint` | 12 / 400, `--scribe` | Help, metadata, dimensions |
| `.ts-label` | 11 / 500 / 78%, `--scribe` | Title block labels |
| `.ts-value` | 12 / 600 | Title block values |
| `.ts-code` | Plex Mono 12 / 400 | Recipes, ids, parameters |

## Space, shape, depth

- Spacing is a 4 px grid: `--s1` 4 · `--s2` 8 · `--s3` 12 · `--s4` 16 · `--s5` 24 · `--s6` 32 · `--s7` 48.
- Two radii: `--r-control` 4 px for buttons and inputs, `--r-panel` 6 px for cards, dialogs and
  popovers. Only dots and badges are round.
- One shadow, `--lift`, and only for things that float above the page: dialogs, the comment
  popover, toasts. Cards and panels are flat with a 1 px `--rule` border.
- Focus is a 2 px `--layout` ring offset from the element (`--focus`), shown for keyboard focus only.

## Icons

[`web/icons.svg`](../web/icons.svg) is a sprite of 24 × 24 line icons with a 1.6 stroke and round
joins. They take their colour from the text around them.

```html
<svg class="i" aria-hidden="true"><use href="/static/icons.svg#i-download"/></svg>
```

In JavaScript, `icon("download")` from `common.js` returns the same markup; `icon("copy", "sm")`
gives the 14 px size. An icon-only button needs `class="icon"` and an `aria-label`.

| Id | Meaning |
|---|---|
| `i-mark` | The autosmith mark: the layered isometric brand mark (64 × 64, multi-tone) |
| `i-search` | Search |
| `i-key` | Passkeys and login security |
| `i-settings` | Admin: print-check settings |
| `i-heart` | Donate button (shown when `DONATE_URL` is set) |
| `i-lock` | Private model (visibility badge and toggle) |
| `i-globe` | Public model, the gallery link |
| `i-grid` | Back to your own library (from the gallery) |
| `i-swatch` | Admin: design system |
| `i-type` | Typography |
| `i-comment` | Comments, comment mode |
| `i-warning` | Print-check warning |
| `i-download` | Download STL |
| `i-play` | The demo video teaser (login page, library footer); drawn filled on the poster |
| `i-copy` / `i-duplicate` | Copy to clipboard / duplicate a model |
| `i-back`, `i-plus`, `i-close`, `i-check` | Navigation and confirmation |
| `i-reply`, `i-restore`, `i-trash`, `i-logout` | Comment reply, restore a revision, delete, log out |
| `i-fit`, `i-spin`, `i-edges`, `i-wire`, `i-spread` | Viewport: fit, auto-rotate, edges, mesh, spread parts |
| `i-dimension` | Overall size readout in the viewport |

New icons follow the same grid: draw inside a 2 px margin, strokes only, no fills (the mark is the
one exception), and keep each to a few paths.

## Brand

Files in [`web/brand/`](../web/brand): `mark.svg` (light/dark aware), `logo.svg` / `logo-dark.svg`
(primary lockup with tagline, text as Plus Jakarta Sans outlines), `icon.svg` (app tile), `og-image.svg|png` (1200 × 630 social
card), PNG app icons, `apple-touch-icon.png` and `site.webmanifest`. The browser tab icon is
[`web/favicon.svg`](../web/favicon.svg) (same as `mark.svg`) with `favicon.ico` as fallback.

- Wordmark: lowercase **auto** in `--graphite` + **smith** in Electric Blue, Plus Jakarta Sans 700. `.eu` is set in `--scribe`, only in lockups.
- Clear space: the height of the spark on every side. Minimum mark size 16 px.
- The mark is traced from the brand board (three layered facets, blue to cyan); do not redraw or recolour it. The sprite symbol `i-mark` carries its own gradients.

## Components

| Class | What it is |
|---|---|
| `header.top` | 56 px bar: wordmark, breadcrumb (`.crumb`), search, live status, icon buttons |
| `.wordmark` | Mark + product name; links home on inner pages |
| `button`, `.btn` | Default control, 32 px high. Modifiers: `.primary`, `.danger`, `.quiet`, `.icon`, `.sm` (26 px), `.big` (44 px), `.on` (pressed toggle) |
| `.seg` | Segmented group: buttons that share borders (camera views, display toggles) |
| `.tb` | Title block: `<dl class="tb"><div><dt>Label</dt><dd>Value</dd></div>…</dl>`. `.tb.full` adds an outer border |
| `.card` | Library item: preview, name, title block (size, revision, updated) |
| `.cbadge`, `.wbadge` | Count badges on a card: open comments (gold), print-check warnings (red) |
| `.vbadge`, `.vbadge.public` | Visibility on a card (bottom right of the thumbnail): Private (outlined, lock) or Public (blue, globe); `.inline` in running text |
| `.live` | Connection dot: grey while connecting, green when live |
| `.banner` | One-line call to action above the library |
| `.sec` | Side panel section with an `h2` heading |
| `.versions` | Revision table: revision, change note, age; selected and current rows are marked |
| `.params` | Parameter editor: mono name, right-aligned value; changed values get the wash |
| `.pin`, `.cpop`, `.cmt` | Comment pin on the model, the popover to write one, a comment in the list |
| `.hud`, `.toast` | Viewport overlays: overall size (bottom left), notifications (bottom right) |
| `.gate .panel` | Centred panel for login, signup, app consent and the privacy page (`.prose`) |
| `.gate .or` | Divider with a word in it ("New here?") between two ways in |
| `ol.start` | Getting-started checklist in the empty library; finished steps get `.done` |
| `.secret` | A value shown only once (a new personal token), with a copy button |
| `dialog` | Modal: the account (name, passkeys, connecting Claude, apps, tokens, deletion) |
| `.tabs` | Links between admin pages; the current one has `aria-current="page"` |
| `.admin` | Settings page layout; `.admin .user` is one account on the Users page |

## Writing

- Name things by what the user sees: *revision*, *part*, *comment*, *recipe*; not *version object*
  or *node*.
- Buttons are verbs and keep their name through the flow: *Rebuild* produces a new revision, the
  toast says what changed.
- Empty states tell you how to start, with a real example.
- Errors state the problem and the fix, without apologising: *Not watertight. The slicer may fill
  or drop faces.*
