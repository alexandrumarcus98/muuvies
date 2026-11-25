---
name: muuvies-testing
description: Automated pytest suite, mocked transport hygiene, Adult Vault stealth test matrix, and feature -> develop -> main Git release workflow. Use when writing tests, debugging regressions, or preparing commits and merges.
---

# Muuvies Testing & Git Release Skill

Enforce these verification gates before any commit, branch merge, or production deployment.

## 1. Automated Verification Gates
Before committing or merging, run:
```bash
(cd frontend && pnpm install --frozen-lockfile && pnpm typecheck && pnpm build)
uv run pytest tests/ -v          # or .venv/bin/pytest -q
docker compose config --quiet
```
Build first: the auth-page tests check that `/login` serves the built app (`backend/static/app/index.html`), so pytest fails on a fresh clone, and can pass against a stale local build. CI runs the same three in this order. `docker compose config` only validates; never run `docker compose up` in the dev checkout on a host running production. When `frontend/` changes, also preview the build (uvicorn on `127.0.0.1:8899` with a temporary `DATA_DIR` and `DATABASE_URL`) and run a headless Chromium check across desktop Dark/Light, mobile `390x844` (`mobileOverflow: false`, zero `pageErrors`) and reduced motion. There are no frontend unit tests; `pnpm typecheck` and `pnpm build` are the gate.

## 2. Test Isolation & Vault Coverage Rules
- **Never hit live external APIs in tests:** Mock `prowlarr_client`, `qb_client`, `*arr` clients, `google.genai`, Azure OpenAI, and GitHub Models using `AsyncMock` / `monkeypatch` and temporary or test SQLite state (`tests/`).
- **Mandatory Vault Dual-Path Coverage:** Any feature touching search results, curator, grabs, queue, history, library mirror, or telemetry must verify behavior for **both** `vault_unlocked=False` (Adult/Whisparr completely stripped, no external LLM calls) and `vault_unlocked=True`.
- Cover fallback behavior explicitly (e.g., Gemini `503`/`429` cascade + cooldown -> Azure OpenAI -> heuristic HTTP `200` degradation).

## 3. Git Branching & Release Workflow
Never commit directly on `main`:
1. Branch from `develop`: `git checkout -b feature-<topic> develop`
2. Verify (the three gates above), then merge cleanly:
   `git checkout develop && git merge --no-ff feature-<topic>`
3. Rebuild and verify your domain (`bash ./scripts/verify_domain.sh <domain>`) before releasing to `main`:
   `git checkout main && git merge --ff-only develop`
