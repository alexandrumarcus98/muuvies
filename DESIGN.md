# Muuvies Design System (`DESIGN.md`)

Big type, quiet screens. Muuvies is a tool for finding and watching films, so in the app the search box is the hero and every screen should read at a glance from the couch. The signed-out landing page is the one place that goes editorial (the 3D Z-axis corridor hero, a giant italic wordmark, film stills). The signed-in app borrows only its accents. Everything in `frontend/src/` follows this file.

The frontend is React 19 + TypeScript, built by Vite into `backend/static/app/` (build output, never edited by hand). Styles are plain CSS: `frontend/src/styles/tokens.css` (tokens, fonts, `bp-*` components), `frontend/src/styles/motion.css` (loading bar, progress fill), `frontend/src/styles/workspace.css` (header vs. phone layout) and `frontend/src/components/corridor/corridor.css` (landing corridor hero, HUD, inspection modal). There is no Tailwind.

## 1. Tokens

Colors are CSS variables in `tokens.css`. Light values sit on `:root`, dark values on `html.dark`; `App.tsx` toggles the `dark` class and stores the choice in `localStorage` (`theme`, dark by default). Use `var(--bp-*)` in CSS and inline styles. Never hard-code hex values in components.

| Token | Dark | Light | Use |
|---|---|---|---|
| `--bp-canvas` | `#0f0000` obsidian | `#f6efed` film paper | Page background, input fill |
| `--bp-surface` | `#1b0d0d` | `#efe6e3` | Cards, modals, mobile tab bar |
| `--bp-raised` | `#261414` | `#ffffff` | Rare: suggestion dropdown |
| `--bp-ink` | `#f6efed` | `#0f0000` | Text, primary buttons, current nav item |
| `--bp-muted` | `#c9bdba` | `#574d4b` | Secondary text, metadata, labels, inactive nav |
| `--bp-line` / `--bp-line-strong` | paper at 16% / 78% | obsidian at 16% / 72% | Decorative hairlines between rows and sections / hover borders |
| `--bp-field` | paper at 42% | obsidian at 46% | Borders of inputs and outlined buttons (3:1 or better) |
| `--bp-red` (+ `--bp-on-red` `#0f0000`) | `#ff2a1a` signal red | same | Fills only: current-nav underline, focus ring, loading bar, vault-open state, destructive confirm |
| `--bp-red-text` | `#ff6a5c` | `#c41a0e` | Red *text* (errors, the AI pick label). Never use `--bp-red` for text |
| `--bp-ok` / `--bp-warn` | `#5fbf8a` / `#e0a64a` | `#256b47` / `#8a5200` | Service status and health only |

**Contrast (WCAG 2.2 AA):** text 4.5:1 or better, large text, icons, focus rings and field borders 3:1 or better, in both themes. Every pair above is checked; re-check with a contrast calculator when you change a token. Grey text on the red burn hero sits on a dark scrim for the same reason.

Font stacks are `--f-sans`, `--f-mono` and `--f-serif`. No other colors: no blues, greens or purples outside `ok`/`warn`. Red stays under about 5% of a screen. The only gradients are the burn hero's pre-WebGL fill and the dark scrims over the hero and the slider still.

## 2. Type

Three self-hosted variable fonts in `backend/static/fonts/`, declared with `@font-face` at the top of `tokens.css`: **Geist**, **Geist Mono** and **Newsreader italic**. Geist is preloaded in `frontend/index.html`.

- **Geist**: everything by default, body at 15–17px, line-height 1.5. Display headings are tight and heavy: `.bp-slider-title` (`clamp(1.75rem, 4.5vw, 3.75rem)`, tracking `-0.04em`) for page and section titles, at a smaller clamp for in-app section cards. The landing wordmarks are bigger still (see Motion).
- **Geist Mono**: only in the burn hero's search and timecode, where it's part of the film look. Everywhere else, figures use Geist with `tabular-nums`.
- **Newsreader italic**: the accent voice. `.bp-serif` for one word in a heading ("Public-domain *prints.*"), `.bp-slider-note` for the line under a slider title, and the italic half of the burn hero lede.

Rules:

- Sentence case for every label, button and heading ("Active downloads", "Quick grab"). Write it that way in the source; no all-caps strings, no tracked eyebrows, no arrows glued to labels.
- A `.bp-kicker` (13px, muted, sentence case) only when it says something the heading doesn't.
- At most one serif accent per heading. Never put the serif on labels, buttons, metadata or form text.
- Nothing below 13px in the app. Titles and file names stay at 15px or larger.

## 3. Components (`bp-*` classes in `tokens.css`)

| Class | Spec |
|---|---|
| `.bp-shell` | Full-height page wrapper on canvas |
| `.bp-nav`, `.bp-brand` (+ `.bp-brand-dot`, 8px red), `.ws-link`, `.ws-tab` (`workspace.css`) | Workspace header: text links, not buttons. Muted until hovered; the current section is ink with a 2px red underline. Below 900px the sections move to a fixed bottom bar (`.ws-tab`, with Vault) and the account links sit behind a Menu link (closes on a pick or Escape). The bottom bar is hidden on desktop |
| `.bp-pill` + `.bp-pill-solid` / `.bp-pill-ghost` / `.bp-pill-accent` / `.bp-pill-active` | Button, 40px min height, 10px radius, Geist 14px/600. Solid (ink) is the one primary action in a group, ghost (outlined) is everything else, accent (red) only for vault-open and destructive confirms, active is the selected segment |
| `.bp-input` | 44px field, 10px radius, `--bp-field` border, red 2px focus outline |
| `.bp-search-form`, `.bp-search-input`, `.bp-search-btn` | The main search: 52px field, 12px radius, with its button |
| `.bp-container` | 1240px max width with fluid side padding |
| `.bp-card` | Tinted well (surface, no border, 14px radius) for a view's header block. Cards inside cards flatten to hairline-separated blocks, and cards that are rows of a `.bp-list` (results, queue, archive) become hairline rows. Don't box individual rows |
| `.bp-row`, `.bp-list`, `.bp-grid-2`, `.bp-grid-3` | Flex row with space-between, vertical stack, auto-fit grids |
| `.bp-modal-backdrop`, `.bp-modal` (+ `.bp-modal-wide`, 1080px) | Dialog scrim (obsidian at 78%, no blur) and 560px panel, 16px radius. Always rendered through `Modal.tsx` |
| `.bp-alert` | Notice for errors and warnings, 10px radius, tinted red |
| `.bp-tag` | Metadata as plain muted text (state, size, category, service). Colour only when it carries state |
| `.bp-kicker`, `.bp-muted`, `.bp-accent`, `.bp-serif`, `.bp-hr` | Label, secondary text, red text (`--bp-red-text`), serif accent, divider |
| `.bp-slider-band`, `.bp-slider-title`, `.bp-slider-note` | Section bands and titles (landing and app) |
| `.bp-loading-bar`, `.bp-progress-fill` (`motion.css`) | Top loading bar; torrent progress fill with an eased width transition |

Shared components: `Modal.tsx` (every dialog), `LoadingBar.tsx`, `components/corridor/*`. Prefer an existing `bp-*` class over new inline styles; add a class to `tokens.css` when a pattern repeats.

## 4. Motion

GSAP 3.15 + ScrollTrigger + `@gsap/react` (`useGSAP`), and Lenis, all from npm. Always import from `frontend/src/motion/gsap.ts`, which registers the plugins once and exports `prefersReducedMotion()`. Motion explains the product or confirms an action. It never hides content for long.

- **Landing** (`LandingPage.tsx`, signed-out only):
  - `useSmoothScroll` runs Lenis on GSAP's ticker, on the landing only; `smoothScrollTo` scrolls through it. The app keeps native scroll. Any inner scroller added to the landing needs `data-lenis-prevent`.
  - `CorridorHero` & `CorridorScene`: Full-width hero backed by a 3D WebGL corridor of 2.5D film stills (`components/corridor/`), driven cleanly via GSAP ScrollTrigger + Lenis.
  - `InspectionModal`: Opens when clicking any film still from the corridor or HUD to inspect metadata and launch search.
  - The hero search sends you to `/login` and keeps the query in `sessionStorage` (`muuvies_pending_q`); the workspace runs it after sign-in.
- **App:**
  - `LoadingBar`: a 2px red bar at the top of the viewport for loads the user asked for (searches, the maintenance report). Never for background polling.
  - Switching tabs fades and rises the main column in (`WorkspaceShell`).
  - `useStaggerIn` fades and rises new rows (results, queue, library, maintenance cards). Rows already on screen don't re-animate; the total stagger is capped at 0.5s.
  - `Modal` fades the backdrop and lifts the panel in, and plays a short exit before unmounting. `useDialogEnter` and `useBackdropDismiss` give the player and the preview lightbox the same behaviour.
- **Reduced motion** (`prefers-reduced-motion`): no Lenis, no burn animation (a still frame and a 100svh hero), no reveals, parallax, staggers or modal animations, no blinking status dot. Everything is visible at once.
- Not allowed: cursor followers in the app, film-grain overlays, app-wide scroll hijacking, and decorative loops that don't show something the app does.
- **Public-page rule:** the landing (and `/login`, `/register`) never names or hints at the vault or adult content (see `plans/adult-vault.md`).

## 5. Dialogs and player

`Modal.tsx` is the only dialog. It closes on Escape, or on a backdrop click only when the press both started and ended on the backdrop (so selecting text in a field and releasing outside doesn't close it). It moves focus to the first field, gives focus back on close, and locks page scroll.

`TheaterModal.tsx` uses the native `<video controls>` plus keyboard shortcuts: `Space` play/pause, `←`/`→` 10s, `J`/`L` 30s, `M` mute, `F` fullscreen, `Esc` close. `hls.js` is imported lazily, only when an HLS stream opens. Audio-track and subtitle (`.vtt`/`.srt`, with offset) controls sit under the video.
