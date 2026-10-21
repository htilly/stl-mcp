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

Named after the workshop materials they come from. Every colour has a light and a dark value.

### Theme

Three modes: **Light**, **Dark** and **Auto** (follow the system, the default). The choice is per
browser (`localStorage["theme"]`) and is set by [`web/theme.js`](../web/theme.js), a classic script
every page loads in `<head>` right after `style.css`, so a page never flashes the wrong theme:

- `auto` leaves `<html>` without `data-theme`, and the dark block applies through
  `prefers-color-scheme`; `light` / `dark` set `<html data-theme="…">`. Every token that changes in
  dark mode is redefined in both dark blocks (tested).
- Any `[data-theme-switch]` element gets the three-way switch (sun / monitor / moon);
  `data-labelled` adds the words. It is in the user menu, in Account settings → Profile, in the
  corner of the login pages and on `/admin/design`.
- Every change, including the system flipping while on auto, dispatches `themechange` on
  `window` (`{theme, effective}`). The 3D view rebuilds its background and grid from it.

| Token | Light | Dark | Use |
|---|---|---|---|
| `--aluminium` | `#f7f8fa` | `#0a0b0e` | Page background |
| `--sheet` | `#ffffff` | `#111318` | Panels, cards, header, dialogs |
| `--inset` | `#f1f3f6` | `#191c23` | Code, hovered rows, the track of segmented controls |
| `--field` | `#ffffff` | `#14171d` | Text inputs and selects |
| `--graphite` | `#0b1220` | `#eceef2` | Text |
| `--scribe` | `#5d6675` | `#959cab` | Secondary text, title block labels |
| `--rule` | `#e5e7eb` | `#23262e` | Borders and title block rules |
| `--rule-strong` | `#d1d5db` | `#343843` | Hovered borders, inactive dots |
| `--layout` | `#2563eb` | `#6aa6ff` | Accent: primary action, selection, focus. Electric Blue (`#3b82f6`, `--brand`) darkened for text contrast |
| `--layout-wash` | `#eaf1ff` | `#15233f` | Selected rows, active toggles, banners |
| `--primary`, `--primary-hover` | `#2563eb`, `#1d4ed8` | `#3b82f6`, `#5b96f7` | Fill of `.primary` buttons (white text) |
| `--fault-wash` | `#fdecea` | `#2c1513` | Hovered destructive buttons |
| `--ember` | `#06b6d4` | `#22d3ee` | Brand Teal: logo gradient end, small accents. Never used for UI state |
| `--pei` | `#e2ad45` | `#e8b552` | Comments and pins. The gold of a PEI build plate |
| `--fault` | `#c0362c` | `#f0735f` | Print-check warnings, destructive actions, errors, the Live dot when offline or the server is unhealthy |
| `--sound` | `#198754` | `#4cc38a` | Live connection with Claude connected, current revision |
| `--wait` | `#c27c0e` | `#f2a93b` | Live dot while the page is live but no AI client has connected over MCP |
| `--stage` | `#eef1f5` | `#0e1015` | 3D viewport background (read by `viewer.html`) |
| `--stage-grid`, `--stage-grid-major` | `#dde2e9`, `#c3cad5` | `#1b1f27`, `#2b313d` | Build plate grid in the viewport |
| `--plate` | `#f6f7f9` | same | Behind preview PNGs; matches `render.BG` |
| `--glass` | white 78 % | `#111318` 72 % | Translucent app bar, viewport toolbars and HUD (with a backdrop blur) |
| `--glow` | blue 16 % | blue 22 % | Soft brand light behind the login pages |
| `--measure` | `#db2777` | same | Measurements clicked in the 3D view: their lines, labels and list badges |
| `--measure-hole`, `--measure-boss` | `#ff8a3c`, `#10b981` | same | Dimensions: rings and labels on round holes and on bosses (dimension lines use `--layout`) |

Rules of use:

- `--layout` is for one thing per view: the primary action or the current selection. Two blue
  buttons side by side is a bug.
- `--pei` always means *a comment*. Do not reuse it for warnings.
- `--measure` always means *a measurement you made*; it is pink so it is never mistaken for a
  comment pin or a hole.
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
- Three radii: `--r-control` 8 px for buttons and inputs, `--r-panel` 12 px for cards, menus and
  popovers, `--r-dialog` 16 px for dialogs and the login panel. Pills, avatars and dots are round.
- Depth comes in steps: `--shadow-xs` (buttons, inputs, cards at rest), `--shadow-sm` (floating
  toolbars, the on segment), `--shadow-md` (hovered cards, sticky action bars) and `--lift` (menus,
  dialogs, toasts, the comment popover). Shadows are darker and borders carry more of the edge in
  dark mode.
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
| `i-gauge` | Admin: live load (CPU, memory) |
| `i-swatch` | Admin: design system |
| `i-type` | Typography |
| `i-comment` | Comments, comment mode |
| `i-warning` | Print-check warning |
| `i-download` | Download STL |
| `i-play` | The demo video link (login page, library footer) |
| `i-copy` / `i-duplicate` | Copy to clipboard / duplicate a model |
| `i-back`, `i-plus`, `i-close`, `i-check` | Navigation and confirmation |
| `i-reply`, `i-restore`, `i-trash`, `i-logout` | Comment reply, restore a revision, delete, log out |
| `i-fit`, `i-spin`, `i-edges`, `i-wire`, `i-spread` | Viewport: fit, auto-rotate, edges, mesh, spread parts |
| `i-dimension` | Overall size readout in the viewport, the Dimensions toggle |
| `i-ruler` | Measure mode in the viewport |
| `i-eye`, `i-eye-off` | Show / hide measurements on the model |
| `i-magnet`, `i-undo` | Measuring bar: Snap, Undo |
| `i-sun`, `i-moon`, `i-monitor` | Theme switch: light, dark, auto |
| `i-user`, `i-chevron` | Account: the user menu button, Profile in settings |
| `i-plug` | AI connections (connecting Claude, ChatGPT, Claude Code) |
| `i-external` | A link that leaves autosmith (GitHub) |
| `i-play` | The demo video |

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
| `header.top` | 56 px translucent app bar: wordmark, primary `.nav` or a `.crumb`, live status, page actions, user menu |
| `.nav` | Primary navigation (My models, Gallery); the current page has `aria-current="page"` |
| `.crumb` | Breadcrumb on inner pages: `My models / <model> / Editor` |
| `.umenu`, `.menu` | User menu (`<div data-usermenu>`, filled by `nav.js`): who you are, Account settings, Passkeys and sign-in, Connect an AI app, Theme, Admin, demo, Privacy, Send feedback, GitHub, Donate, Log out |
| `.theme-switch` | Light / auto / dark switch (`[data-theme-switch]`, rendered by `theme.js`) |
| `.page-head` | Title row inside a page: heading, count, search and the page's one primary action |
| `.subnav` | Second bar under the app bar holding `.tabs` (admin pages) |
| `.wordmark` | Mark + product name; links home on inner pages |
| `button`, `.btn` | Default control, 34 px high. Modifiers: `.primary`, `.danger`, `.quiet`, `.icon`, `.sm` (28 px), `.big` (44 px), `.on` (pressed toggle) |
| `.seg` | Segmented group: buttons on a shared track; the pressed one (`.on`) is raised (camera views, display toggles, editor tool groups) |
| `.tb` | Title block: `<dl class="tb"><div><dt>Label</dt><dd>Value</dd></div>…</dl>`. `.tb.full` adds an outer border |
| `.card` | Library item: preview, name, title block (size, revision, updated) |
| `.cbadge`, `.wbadge` | Count badges on a card: open comments (gold), print-check warnings (red) |
| `.vbadge`, `.vbadge.public` | Visibility on a card (bottom right of the thumbnail): Private (outlined, lock) or Public (blue, globe); `.inline` in running text |
| `.live` | Connection pill: grey while connecting, green when an AI client is connected, amber while waiting for one, red when offline |
| `.banner` | One-line call to action above the library |
| `.sec` | Side panel section with an `h2` heading |
| `.versions` | Revision table: revision, change note, age; selected and current rows are marked |
| `.params` | Parameter editor: mono name, right-aligned value; changed values get the wash |
| `.pin`, `.cpop`, `.cmt` | Comment pin on the model, the popover to write one, a comment in the list |
| `.hud`, `.toast` | Viewport overlays: overall size (bottom left), notifications (bottom right) |
| `.mtag`, `.mbar` | Measuring (`web/measure.js`): labels on the model, the Distance/Faces/Thickness/Angle bar (bottom centre) |
| `.gate .panel` | Centred panel for login, signup, app consent and the privacy page (`.prose`) |
| `.gate .or` | Divider with a word in it ("New here?") between two ways in |
| `ol.start` | Getting-started checklist in the empty library; finished steps get `.done` |
| `.secret` | A value shown only once (a new personal token), with a copy button |
| `dialog`, `.dlg-head` | Modal with a title bar and a close button |
| `.feedback-dlg`, `.fb-body` | Feedback dialog opened from the user menu (`feedback.js`): write, review Claude's draft of the issue, done. Only shown when an admin has set up the feedback button |
| `.settings-dlg`, `.settings` | Account settings: a side list of panes, one shown at a time. **Profile** (name, usage, appearance, delete account), **Sign-in & security** (passkeys, linked sign-ins), **AI connections** (connect wizard, connected apps, personal tokens). Opened from the user menu with `/#account`, `/#security`, `/#connect` |
| `.tabs` | Links between admin pages in the `.subnav`; the current one has `aria-current="page"` |
| `.admin` | Settings page layout; `.admin .user` is one account on the Users page |

## Phones and touch

- **Touch** (`pointer: coarse`): buttons are 40 px high (`.sm` 34 px), menu rows 44 px, and inputs
  use 16 px text so iOS does not zoom in when one is focused. Hover-only effects, such as the card
  lift, sit behind `@media (hover: hover)` so a tap does not leave them stuck.
- **Up to 860 px**: the 3D view comes first, then its side panel below it. Viewport toolbars (viewer
  and editor) stay one row that scrolls sideways instead of wrapping over the model. Account
  settings becomes a full-screen sheet with three tabs (*Profile*, *Security*, *AI apps*); Log out
  is in the user menu. A user's facts on the admin Users page wrap into rows of three.
- **Up to 520 px**: the wordmark shows only the mark, breadcrumbs keep just the current name,
  header buttons with a `.label` become icon-only, and the editor's *Unsaved changes* becomes an
  amber dot beside Save.
- No page scrolls sideways at 375 px.

## Writing

- Name things by what the user sees: *revision*, *part*, *comment*, *recipe*; not *version object*
  or *node*.
- Buttons are verbs and keep their name through the flow: *Rebuild* produces a new revision, the
  toast says what changed.
- Empty states tell you how to start, with a real example.
- Errors state the problem and the fix, without apologising: *Not watertight. The slicer may fill
  or drop faces.*
