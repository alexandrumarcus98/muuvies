---
name: muuvies-deploy-ops
description: Production deploys through scripts/deploy.sh, the two-stage Docker build (React frontend + FastAPI), reverse proxy TLS and /static routing, host network binding checks, and SQLite WAL + Fernet backups. Use when editing Dockerfile, .dockerignore, docker-compose.yml, scripts/, or deploying changes.
---

# Muuvies Deployment & Ops Skill

Use these procedures when building containers, updating reverse proxy config, or backing up persistent state.

## 1. Builds and Deploys
- **The image builds the frontend.** `Dockerfile` has a `node:22-alpine` `frontend-builder` stage (`pnpm install --frozen-lockfile && pnpm build` in `frontend/`), then copies `backend/static/app` into the Python image. `.dockerignore` excludes any local `backend/static/app`, so a stale local build can never ship.
- **Keep production separate from the dev checkout.** Configure `git config muuvies.production true` only in the production checkout so `scripts/deploy.sh` refuses to run from a development working tree. CI runs pytest, `pnpm install --frozen-lockfile && pnpm typecheck && pnpm build`, and `docker compose config --quiet`, and triggers `scripts/deploy.sh` only when `vars.DEPLOY_ENABLED == 'true'`.
- **Never run `docker compose up`, `down`, or `restart` in a dev checkout on the same host as production** (both share `name: muuvies`). `docker compose config --quiet` is safe.
- `scripts/deploy.sh` backs up data, checks out the commit, and rebuilds only `media-search` (`--no-deps`), so `seedbox-tunnel` stays up. Its health check passes only if:
  1. `http://127.0.0.1:8888/api/health` (and `PUBLIC_HEALTH_URL` if configured) reports `{"status":"healthy"}`;
  2. when `PUBLIC_HEALTH_URL` is set, the public page loads and its `/static/app/assets/*.js` entry script downloads.

  Otherwise it rolls back to the previous image and commit. Manual use: `--status`, `--rollback`, `<sha>`.

## 2. Reverse Proxy and `/static`
When fronting `media-search` with a reverse proxy (e.g., Caddy on a shared external `proxy-net` Docker network):
- Proxy `/static/app/*` to `media-search:8888` with `Cache-Control: public, max-age=31536000, immutable` (Vite filenames are content-hashed).
- Serve static `fonts/` and `media/` from `backend/static` (read-only) with immutable caching, and route remaining paths to `media-search:8888`.

## 3. Network & Blast-Radius Verification
Confirm internal ports never leak onto `0.0.0.0`:
- `media-search`: binds only `${APP_BIND_HOST:-127.0.0.1}:8888`.
- `seedbox-tunnel`: binds only `127.0.0.1` (`2468`, `7878`, `8989`, `QB_PORT`, `PROWLARR_PORT`, `WHISPARR_PORT`).

Check bindings and container health:
```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
curl -fsS http://127.0.0.1:8888/api/health
bash ./scripts/verify_domain.sh <your-domain>
```

## 4. Safe SQLite WAL + Fernet Key Backups
`EncryptedSetting` rows in `data/media_search.db` are encrypted with `data/.fernet_key`—never back up one without the other, and never raw-`cp` a live WAL database.

Always run [backup_data.sh](scripts/backup_data.sh) before schema, key, or volume changes:
```bash
bash ./scripts/backup_data.sh
```
This uses Python's `sqlite3.Connection.backup()` to snapshot WAL safely alongside `data/.fernet_key`, stores a `chmod 600` `.tar.gz` archive (default `~/.local/backups/muuvies/` or `~/backups/muuvies/`), and retains the newest 7 archives.
