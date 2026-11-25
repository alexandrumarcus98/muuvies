---
name: muuvies-frontend
description: React 19 + TypeScript + Vite frontend in frontend/src, the Vite build into backend/static/app, bp-* design tokens, GSAP/Lenis motion with reduced-motion rules, the shared Modal, AbortController cancellation, prefetch and grab button locks. Use whenever editing frontend/, DESIGN.md, or anything served under /static.
---

# Muuvies Frontend Skill

Follow these rules when changing components, styles, motion or the frontend build.

## 1. Source vs. Build Output
- **Source lives only in `frontend/src/`** (`package.json`, `vite.config.ts` and `index.html` sit in `frontend/`):
  - `App.tsx`: `/api/auth/me` check, theme (`html.dark`, `localStorage` `theme`), lazy routes for `LandingPage`, `AuthPage` (login, register, reset, passkeys) and `WorkspaceShell`.
  - Views: `SearchView`, `IdleLibraryView`, `DownloadsView`, `MaintenanceView`, `AdminView`, `TheaterModal` (player), `VaultPasskeyModals`.
  - Shared: `components/Modal.tsx` (every dialog; also exports `useBackdropDismiss` and `useDialogEnter`), `components/LoadingBar.tsx`, `lib/api.ts` (`apiFetch`, `ApiError`), `types/api.ts`.
  - Landing: `components/corridor/` (3D Z-axis corridor hero & diorama showcase).
  - Styles: `styles/tokens.css` (tokens, `@font-face`, `bp-*` classes), `styles/motion.css`, `styles/workspace.css`, `components/corridor/corridor.css`.
  - Motion: `motion/gsap.ts`, `motion/useSmoothScroll.ts`, `motion/useStaggerIn.ts`.
- **`backend/static/app/` is Vite build output.** It is gitignored and built for production only inside the Docker image (`frontend-builder` stage; `.dockerignore` excludes any local copy). Never commit or hand-edit it.
- `backend/static/` in git holds only `fonts/` (Geist, Geist Mono, Newsreader italic) and `media/` (landing stills). Everything there is public: never put seedbox details in it (`test_static_assets_contain_no_seedbox_details`).
- There is no Tailwind, no `?v=` cache-busting and no `node --check`. Vite hashes file names; FastAPI serves `index.html` no-cache.

## 2. Design and Motion Rules (`DESIGN.md`)
- Colors come from `--bp-*` tokens in `tokens.css`; reuse the `bp-*` classes before adding inline styles.
- Import GSAP only from `motion/gsap.ts` (registers ScrollTrigger and `useGSAP` once, exports `prefersReducedMotion`). Use `useGSAP` so tweens are cleaned up on unmount.
- Lenis (`useSmoothScroll`) runs on the landing only. The workspace keeps native scroll.
- `LoadingBar` is for loads the user started (searches, the maintenance report), not background polling.
- `useStaggerIn` animates only rows not shown before; keep that so polling doesn't re-animate lists.
- Every dialog uses `Modal` (Escape, backdrop press that starts and ends on the backdrop, focus in and out, scroll lock, enter/exit animation). The player and preview lightbox reuse its hooks.
- Under `prefers-reduced-motion`: no Lenis, a static catalog grid, no reveals, staggers or modal animations; everything visible at once.
- The landing, `/login` and `/register` never name or hint at the vault or adult content.
- `hls.js` stays a lazy `import('hls.js')` in `TheaterModal`, loaded only when an HLS stream opens.

## 3. Network and UI State Hygiene
- **Cancellation:** abort in-flight searches, suggestions, prefetches and preview fetches with `AbortController` on new queries, keystrokes and unmount.
- **Prefetch:** a highlighted suggestion warms the search cache after 180 ms with `X-Prefetch: 1`, so it doesn't pollute search logs. Player manifests are prefetched and cached for 60 s in `WorkspaceShell`.
- **Polling:** telemetry and queue poll every 5 s in `WorkspaceShell` and pause while `document.hidden`.
- **Grab locks:** Quick Grab and "Send to *arr" are disabled while a grab for that row is in flight (`grabbingMap`) and when `can_grab` is false. A grabbed row (`already_grabbed` or grabbed this session) reads GRABBED but is not disabled yet.
- **Vault lock:** when the vault locks, `SearchView` aborts preview fetches, clears preview images and the lightbox, and drops adult rows.

## Verification
```bash
cd frontend && pnpm typecheck && pnpm build
.venv/bin/pytest -q
```
Then preview with uvicorn on `127.0.0.1:8899` (temporary `DATA_DIR` and `DATABASE_URL`; FastAPI serves the build) or `pnpm dev` (Vite on `127.0.0.1:5173`, proxying `/api` to `127.0.0.1:8000`). Never run `docker compose up` in a dev checkout on the same host as production. Check desktop dark and light, mobile `390x844` (no horizontal overflow, zero page errors) and reduced motion.
