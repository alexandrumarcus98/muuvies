---
name: muuvies-backend
description: Backend architecture, FastAPI routers, SQLite WAL/FTS5, HTTPX connection pools, Adult Vault privacy, qBittorrent uploads, and AI Curator cascade rules. Use whenever editing files under backend/app/.
---

# Muuvies Backend Skill

Follow these conventions when modifying FastAPI routers, database models, or external service clients in `backend/app/`.

## 1. ASGI Loop, Serialization & Concurrency
- **Default Serializer:** `FastAPI(..., default_response_class=ORJSONResponse)` is enabled in [main.py](backend/app/main.py). Keep all route handlers `async def` so CPU/I/O work never stalls the `uvloop` event loop.
- **No Blocking I/O in Request Path:** Offload analytics/audit writes (`SearchLog`) via FastAPI `BackgroundTasks` and keep heavy library syncing in the background `library_mirror` loop (`600s`) rather than HTTP request paths.

## 2. Isolated Outbound HTTP/2 Pools
- Route **all** outbound HTTP requests through the shared pools in [http_clients.py](backend/app/services/http_clients.py):
  - `get_search_client()` (`15.0s` read timeout): Prowlarr searches, `.torrent` downloads, `*arr` additions, and AI Curator calls (Azure OpenAI / GitHub Models).
  - `get_telemetry_client()` (`1.5s` read timeout): Telemetry polling, qBittorrent queue/transfer checks, and fast service health probes.
- **Never** instantiate per-request `httpx.AsyncClient(...)` inside routers or services.

## 3. SQLite WAL, FTS5 Mirror & Cache
- Runtime storage uses `data/media_search.db` with `WAL` journal mode configured in [database.py](backend/app/database.py).
- Library lookups read exclusively from local FTS5 tables (`library_mirror.py`) and in-memory SWR cache (`services/cache.py`: `search_cache`, `curator_cache`) so searches stay responsive even when external *arr APIs stall.
- Keep `media-search` single-worker (`--workers 1`) unless `SearchMemoryCache`, rate limiters (`security.py`), and the AI cooldown circuit breaker are moved to a shared store.

## 4. Progressive AI Curator Cascade
Implemented in [ai_assistant.py](backend/app/ai_assistant.py) and [search.py](backend/app/routers/search.py):
- Cascade order (`AI_PROVIDER=auto`): **Gemini (`gemini-3.5-flash-lite`)** -> **Azure OpenAI (`gpt-5-mini`)** -> **GitHub Models** -> **0ms Local Heuristic Rules Engine**.
- Catch `google.genai.errors.APIError` (`503`/`429`), `asyncio.TimeoutError`, and unexpected exceptions; record Sentry breadcrumbs and trip the **60-second `_gemini_cooldown_until` circuit breaker** on capacity/rate errors so subsequent queries skip directly to Azure.
- `/api/search/curator` must **always** degrade gracefully with HTTP `200` (`CuratorResponse`), falling back to heuristic scoring if all external LLMs fail.

## 5. Seedbox / qBittorrent Integration
- Remote qBittorrent cannot resolve Docker internal hostnames like `seedbox-tunnel`. In [qbittorrent.py](backend/app/services/qbittorrent.py), resolve HTTP(S) torrent links server-side inside `media-search` (following `magnet:` redirects cleanly) and upload `.torrent` bytes via multipart `files={"torrents": ...}` rather than raw internal URLs.
- Support both qBittorrent v5 (`/torrents/stop`, `/torrents/start`, `stopped`) and v4 (`/torrents/pause`, `/torrents/resume`) in `control_torrents()`.

## 6. Defense-in-Depth Adult Vault Privacy
- Enforce `check_vault_unlocked(session)` server-side across **all** layers: `/api/search`, `/api/search/suggestions`, `/api/search/curator`, `/api/grab/*`, `/api/queue`, `/api/grab/history`, `/api/library` (`whisparr` items), and [observability.py](backend/app/observability.py) Sentry event/header/secret scrubbing.
- Never forward Adult Vault queries or candidate titles to external LLM providers.

## Verification
```bash
uv run pytest tests/ -v
```
