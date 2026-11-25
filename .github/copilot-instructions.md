# Engineering Guardrails for Muuvies

When reviewing or writing code in this repository, enforce these non-negotiable architectural invariants:

## 1. Adult Vault 404 Stealth Isolation
- All search, suggestion, curator, queue, library mirror, telemetry, and stream manifest endpoints MUST check `check_vault_unlocked(session)`.
- When `vault_unlocked` is `False`, Adult/Whisparr/Vault-indexer entries must be stripped cleanly or return **HTTP `404`** (never `403`, which confirms existence).
- Adult titles and Whisparr items must never be sent to external LLM providers (Gemini, Azure OpenAI, GitHub Models) or included in non-vault Watchdog email alerts.

## 2. Async FastAPI & `<8ms` SQLite WAL / FTS5 Performance
- Every FastAPI endpoint performing network or database I/O must be `async def`.
- Reuse the persistent HTTPX connection pools in `backend/app/services/http_clients.py` (`get_search_client()`, `get_telemetry_client()`). Never allocate per-request `httpx.AsyncClient` instances.
- Run Uvicorn single-worker (`--workers 1`) so SQLite WAL pragmas and in-memory SWR caches (`SearchMemoryCache`, `_USER_TOKEN_VERSION_FLOORS`) stay consistent without lock contention.

## 3. Session & Password Revocation (`token_version`)
- Password changes/resets and account suspensions must increment `User.token_version` in SQLite and call `set_user_token_version(user.username, next_version)`.
- Server-side auth routes (`/login`, `/register`) and `get_current_session` must respect expiry, `is_active`, and `token_version`.
- Under `REQUIRE_EMAIL_VERIFICATION`, unverified logins must return HTTP `403` with `{"detail": "Email not verified", "code": "EMAIL_UNVERIFIED"}`.

## 4. React Frontend Source & Build Output
- The frontend is React 19 + TypeScript + Vite. Source lives exclusively in `frontend/src/`; `frontend/package.json` and `vite.config.ts` drive the build.
- `pnpm build` writes to `backend/static/app/`. That folder is build output: gitignored, built for production only inside the Docker image (`frontend-builder` stage), and never committed or hand-edited. `backend/static/` in git holds only `fonts/` and `media/`.
- Vite hashes asset file names, so there are no `?v=` strings to bump. A reverse proxy can serve `/static/app/*` from `media-search` with immutable cache headers; FastAPI serves `index.html` no-cache.
- Before calling frontend work done, run `cd frontend && pnpm typecheck && pnpm build` (CI runs the same after pytest).
- Import GSAP only from `frontend/src/motion/gsap.ts`. Every dialog goes through `components/Modal.tsx`.
- Test fixtures in `tests/conftest.py` must keep `DATABASE_URL` and `DATA_DIR` isolated to a temporary directory so `pytest` never mutates `./data/media_search.db`.

## 5. Network Binding & Safe Container Updates
- Keep internal port bindings restricted (`127.0.0.1` or an explicit `APP_BIND_HOST` such as a private VPN interface) — never `0.0.0.0` outside your TLS reverse proxy (`80`/`443`).
- Back up SQLite WAL + `.fernet_key` via `./scripts/backup_data.sh` before schema/key changes and update `media-search` selectively (`docker compose up -d --no-deps media-search`) so `seedbox-tunnel` remains uninterrupted.

## 6. Visual System (`DESIGN.md`)
- All UI in `frontend/src/` MUST conform to [DESIGN.md](../DESIGN.md): `--bp-*` color tokens from `frontend/src/styles/tokens.css` only (`canvas`, `surface`, `raised`, `ink`, `muted`, `line`, `line-strong`, `red`, `ok`, `warn`; no raw hex in components), Geist / Geist Mono / Newsreader italic as specified, the `bp-*` component classes (`.bp-pill`, `.bp-input`, `.bp-card`, `.bp-tag`, `.bp-kicker`, `.bp-modal`), at most one serif accent per heading, and no purple/violet, cursor followers in the app, film grain or app-wide smooth-scroll (Lenis runs on the landing only). Motion rules, including reduced motion, are in DESIGN.md section 4.
- The public landing and auth pages never name or hint at the vault or adult content.
