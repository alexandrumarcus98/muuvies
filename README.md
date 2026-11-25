# ⚡ Muuvies: Unified Media Search, AI Release Assistant & Seedbox Control Plane

[![Muuvies CI](https://github.com/alexandrumarcus98/muuvies/actions/workflows/ci.yml/badge.svg)](https://github.com/alexandrumarcus98/muuvies/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A self-hosted unified media search engine, AI release curation assistant, and seedbox control plane. Built with Python 3.13, FastAPI, SQLite (`aiosqlite` + SQLAlchemy async), Google GenAI SDK (`google-genai`), and a React 19 + TypeScript frontend built with Vite.

Designed to run locally or on a VPS via **Docker Compose** or native **systemd user services**, and connect securely to your remote **seedbox (`SEEDBOX_HOST`)** through an encrypted `autossh` multiplexed tunnel.

---

## 🌟 Key Features

### 1. Unified Search & Multi-Service Control Plane
- **Prowlarr Integration:** Real-time search across indexers for Movies, TV Shows, and Adult releases.
- **Live Seedbox Telemetry Header:** Persistent status monitoring for qBittorrent, Prowlarr, Radarr, Sonarr, Whisparr, and `cross-seed`, active download/upload speeds, seedbox disk free space, and (for admins) one-click web UI launchers.
- **Dual-Mode Grabbing:**
  - **Quick Grab:** Pushes releases directly into qBittorrent mapped to categories `Movies`, `TV Shows`, or `adult` (automatically triggering `cross-seed` matching on completion).
  - **Monitor in \*arr:** Dispatches to Radarr, Sonarr, or Whisparr to manage automated metadata tracking and hardlinking into your configured `*_ROOT_FOLDER` directories (e.g. `/home/<seedbox-user>/media/{Movies,TV Shows,adult}`).
- **Live Queue Drawer:** Real-time qBittorrent download progress, speed, ETA, and admin torrent controls (pause, resume, delete).

### 2. Multi-Provider "AI Pick" Release Curator
- Powered by Google GenAI SDK (`google-genai` using `gemini-3.5-flash-lite` by default), with automatic fallback to Azure OpenAI (`gpt-5-mini`), GitHub Models, and the built-in heuristic rules engine.
- **Natural Language Parsing:** Understands queries like *"Dune 2 4K HDR remux high seeders Framestor"*.
- **Release Quality Scoring & Analysis:** Evaluates scene vs. reputable P2P groups (Framestor, playBD, FLUX, NTb, CMRG, etc.), source fidelity (Remux > BluRay > WEB-DL >> CAM), video/audio codecs (Dolby Vision, HDR10+, TrueHD Atmos, DTS-HD MA), and seeder health. Optional `PRIVATE_TRACKERS` boosts configured private indexers.
- **Duplicate Detection:** Cross-references Radarr/Sonarr/Whisparr libraries to warn if a title is already in your media collection.
- **Clear AI Recommendation:** Prominently highlights the single best release with a concise justification.

### 3. Server-Side Adult Vault & PIN Lock ("Stealth Mode")
- Protects the `Adult` category (`Whisparr`, `adult` qBittorrent category, adult search results, and grab history) behind a secondary PIN or WebAuthn passkey.
- **True Server-Side Protection:** When locked, the FastAPI backend strips all adult items, Whisparr telemetry, and adult downloads from API responses and returns `404` on locked Vault endpoints (not merely hidden in CSS or localStorage).
- Pre-download cover/thumbnail previews and custom vault indexer keywords are opt-in via environment variables (`ADULT_INDEXER_KEYWORDS`, `ADULT_PREVIEW_SOURCES_JSON`, `ADULT_PREVIEW_IMAGE_HOSTS`, `ADULT_PREVIEW_IMAGE_SUBDOMAIN_HOSTS`) and fail closed when unset.
- Configurable auto-relock timeout (default 30 minutes).

### 4. Security & Cryptographic Hygiene
- Session authentication with Argon2id password hashing, signed `HttpOnly; SameSite=Strict` cookies, and persistent SQLite `token_version` / `is_active` session revocation.
- Brute-force login rate limiting with bounded sliding-window tracking.
- Fernet-encrypted runtime setting overrides (`encrypted_settings` SQLite table) and encrypted stream tokens.

---

## 🏗️ Architecture & Port Mapping

```text
                                      +---------------------------------------------+
                                      |   Remote Seedbox                            |
                                      |   Host: <SEEDBOX_HOST>                      |
                                      |   User: <SEEDBOX_USER>                      |
                                      |   Storage: /home/<SEEDBOX_USER>             |
                                      +---------------------------------------------+
                                      | Tunneled Services (configurable via .env):  |
                                      |  • 8080: qBittorrent WebUI (QB_PORT)        |
                                      |  • 9696: Prowlarr Indexer (PROWLARR_PORT)   |
                                      |  • 6969: Whisparr Adult (WHISPARR_PORT)     |
                                      |  • 2468: cross-seed Daemon                  |
                                      |  • 7878: Radarr Movies                      |
                                      |  • 8989: Sonarr TV Shows                    |
                                      +---------------------------------------------+
                                                             ^
                                                             | autossh multiplexed tunnel
                                                             | (ServerAliveInterval=15)
                                                             v
+-----------------------------------------------------------------------------------+
| Docker Compose or Native Linux Systemd                                            |
|                                                                                   |
|  +------------------------+             +---------------------------------------+ |
|  |  seedbox-tunnel        |             |  media-search (FastAPI + GenAI SDK)   | |
|  |  (autossh container)   | <---------> |  Port 127.0.0.1:8888                  | |
|  |  Mounts seedbox-ssh:ro |             |  Storage: ./data/media_search.db      | |
|  +------------------------+             +---------------------------------------+ |
|             |                                                |                    |
|             v                                                v                    |
|  Exposes 127.0.0.1:{8080, 9696, 6969, 2468, 7878, 8989}      Editorial SPA        |
+-----------------------------------------------------------------------------------+
```

---

## 🚀 Quick Start (Docker Compose)

### 1. Prerequisites
- Docker & Docker Compose (`docker compose version`)
- SSH key configured to connect to `<SEEDBOX_USER>@<SEEDBOX_HOST>` (in `~/.config/muuvies/seedbox-ssh/` with `known_hosts`)

### 2. Configure Environment
```bash
cp .env.example .env
nano .env
```
Fill in your API keys and credentials:
- `SECRET_KEY`, `ADMIN_PASSWORD`, `VAULT_PIN`, `QBITTORRENT_PASSWORD` (rotate all defaults before exposing).
- `GEMINI_API_KEY` (or Azure OpenAI / GitHub Models).
- `PROWLARR_API_KEY`, `RADARR_API_KEY`, `SONARR_API_KEY`, `WHISPARR_API_KEY`, `CROSS_SEED_API_KEY`.
- `TMDB_READ_ONLY_API_KEY` (optional, for landing showcase metadata).

### 3. Build & Run Containers
```bash
docker compose up -d --build
```
The image builds the React frontend in a `node:22-alpine` stage (`frontend-builder` in `Dockerfile`), so the host needs no Node.js. Note: `docker-compose.yml` references an external `proxy-net` bridge network for reverse-proxy integration (`docker network create proxy-net` if you don't already have one).

### 4. Verify Services
```bash
# Check container status
docker compose ps

# View live application logs
docker compose logs -f media-search

# Test backend health check
curl -f http://127.0.0.1:8888/api/health
```

Open your browser at **[http://127.0.0.1:8888](http://127.0.0.1:8888)** to log in and start searching!

---

## 🔒 Adult Vault ("Stealth Mode")

The **Adult category**, **Whisparr telemetry**, **qBittorrent adult queue items**, and **adult grab history** are server-side gated:

1. **Locked ("Stealth Mode"):**
   - Adult category tab is hidden.
   - Prowlarr queries strip adult categories on the server.
   - Adult torrents in qBittorrent queue are stripped from API output.
   - Whisparr status and web UI links are suppressed.
2. **Unlocking:**
   - Click the **"Stealth Mode"** button in the header.
   - Enter your Vault PIN or unlock with a WebAuthn passkey.
   - The session unlocks for 30 minutes (configurable via `VAULT_TIMEOUT_MINUTES`).
   - Click **"Adult Vault"** anytime to immediately re-lock.

---

## 🚢 Deploying

Run production from a dedicated server checkout (separate from any dev checkout, with its own `.env` and `data/` directory holding SQLite + `.fernet_key`).

### Automatic: push to `main`

Every push to `main` runs the `verify` job in `.github/workflows/ci.yml`. When `vars.DEPLOY_ENABLED == 'true'`, the `deploy` job joins the server's WireGuard network as the CI peer from `DEPLOY_WG_CONFIG`, then SSHes to `vars.DEPLOY_HOST` with a key pinned to `scripts/deploy.sh --ssh`. The script:

1. refuses any commit that is not on `origin/main`;
2. runs `./scripts/backup_data.sh` (stores backups in `~/backups/muuvies`, newest 7 kept);
3. checks out the commit and runs `docker compose up -d --build --no-deps media-search`, so `seedbox-tunnel` and any external reverse proxy keep running;
4. waits for `{"status":"healthy"}` from `http://127.0.0.1:8888/api/health` (and `PUBLIC_HEALTH_URL` if configured).

If the health check fails, the script rolls back to the previous image and commit and fails the job with recent container logs.

### Manual deploy and rollback (on the server)

```bash
git config muuvies.production true                                        # one-time mark on prod checkout
git fetch origin && ./scripts/deploy.sh "$(git rev-parse origin/main)"   # deploy the tip of main
./scripts/deploy.sh --rollback                                          # go back to the previous good deploy
./scripts/deploy.sh <sha>                                               # deploy any commit that is on main
./scripts/deploy.sh --status                                            # deploy history
```

> **Note:** On a host running production, avoid `docker compose up` inside a separate dev checkout (both share `name: muuvies`).

### CI Deploy Setup (Optional)
1. Prepare the dedicated production checkout with `git config muuvies.production true`, `.env`, `data/`, and `~/.config/muuvies/seedbox-ssh/{id_ed25519,known_hosts}`.
2. Optional WireGuard CI peer: `sudo deploy/add-ci-wireguard-peer.sh [ci-wg-ip]` (uses `WG_ENDPOINT_HOST`).
3. Configure GitHub repository secrets `DEPLOY_SSH_KEY`, `DEPLOY_WG_CONFIG`, `DEPLOY_KNOWN_HOSTS` and repository variables `DEPLOY_HOST`, `DEPLOY_DOMAIN`, `DEPLOY_USER`, and `DEPLOY_ENABLED=true`.

---

## 🎨 Frontend

The app is React 19 + TypeScript + Vite in `frontend/` (source in `frontend/src/`, design rules in `DESIGN.md`). `pnpm build` writes to `backend/static/app/`, which is gitignored and built for production only inside the Docker image. FastAPI serves `backend/static/app/index.html` (no-cache) for `/`, `/login`, `/register` and `/reset-password`; the hashed assets under `/static/app/` are cached as immutable. `backend/static/` in git holds only `fonts/` and `media/`.

Preview locally without touching Docker containers:

```bash
(cd frontend && pnpm build)
tmp=$(mktemp -d)
DATA_DIR=$tmp DATABASE_URL="sqlite+aiosqlite:///$tmp/media_search.db" \
  .venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8899
```

Or run `cd frontend && pnpm dev` for Vite with hot reload on `127.0.0.1:5173`; it proxies `/api` to `127.0.0.1:8000`.

---

## 🧪 Testing & Verification

Typecheck and build the frontend first, then run pytest (the auth-page tests check that `/login` serves the built app):

```bash
(cd frontend && pnpm install --frozen-lockfile && pnpm typecheck && pnpm build)
.venv/bin/pytest -v
docker compose config --quiet
```

---

## 📄 License & Attribution

- Code is licensed under the [MIT License](LICENSE).
- Bundled typefaces (`Geist`, `Geist Mono`, `Newsreader`) are licensed under the SIL Open Font License 1.1 (see [backend/static/fonts/OFL.txt](backend/static/fonts/OFL.txt)).
- This product uses the TMDB API but is not endorsed or certified by TMDB.
