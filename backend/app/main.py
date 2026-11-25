import asyncio
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, ORJSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import settings
from backend.app.database import init_db, AsyncSessionLocal, get_db
from backend.app.middleware.api_guard import APIGuardMiddleware
from backend.app.models import EncryptedSetting, User
from backend.app.observability import init_sentry
from backend.app.security import decode_session_token, decrypt_text, set_user_token_version
from backend.app.services.cache import run_catalogue_prewarm_loop
from backend.app.services.http_clients import close_http_clients, get_preview_client, init_http_clients
from backend.app.services.library_mirror import ensure_library_mirror_schema, run_library_sync_loop
from backend.app.services.streaming import close_sftp_pool
from backend.app.services.watchdog import run_watchdog_alert_loop
from backend.app.routers import (
    admin_users,
    adult_preview,
    auth,
    vault,
    search,
    grab,
    queue,
    telemetry,
    maintenance,
    library,
    public_media,
    stream,
    settings as settings_router,
)


def _should_enable_sentry() -> bool:
    """Never initialize production Sentry while running under pytest or ENVIRONMENT=test."""
    return (
        bool((settings.SENTRY_DSN or "").strip())
        and "pytest" not in sys.modules
        and os.getenv("ENVIRONMENT", "").strip().lower() != "test"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Initialize SQLite Database & Models
    await init_db()

    # 2. Load runtime encrypted setting overrides
    try:
        async with AsyncSessionLocal() as session:
            stmt = select(EncryptedSetting)
            res = await session.execute(stmt)
            for item in res.scalars().all():
                try:
                    val = decrypt_text(item.encrypted_value)
                    if item.key == "gemini_api_key":
                        settings.GEMINI_API_KEY = val
                    elif item.key == "github_token":
                        settings.GITHUB_TOKEN = val
                    elif item.key == "ai_provider":
                        settings.AI_PROVIDER = val
                    elif item.key == "prowlarr_api_key":
                        settings.PROWLARR_API_KEY = val
                    elif item.key == "radarr_api_key":
                        settings.RADARR_API_KEY = val
                    elif item.key == "sonarr_api_key":
                        settings.SONARR_API_KEY = val
                    elif item.key == "whisparr_api_key":
                        settings.WHISPARR_API_KEY = val
                    elif item.key == "cross_seed_api_key":
                        settings.CROSS_SEED_API_KEY = val
                    elif item.key == "qbittorrent_password":
                        settings.QBITTORRENT_PASSWORD = val
                    elif item.key == "vault_pin":
                        settings.VAULT_PIN = val
                    elif item.key == "sentry_dsn":
                        settings.SENTRY_DSN = val
                except Exception:
                    pass
    except Exception:
        pass

    # 3. Warn on insecure default secrets in production
    _startup_logger = logging.getLogger("muuvies.startup")
    if "pytest" not in sys.modules and os.getenv("ENVIRONMENT", "").strip().lower() != "test":
        if settings.SECRET_KEY.startswith("dev-secret-key"):
            _startup_logger.warning("SECRET_KEY is using the default dev placeholder; set a strong SECRET_KEY in .env.")
        if settings.ADMIN_PASSWORD == "admin123":
            _startup_logger.warning("ADMIN_PASSWORD is still set to default 'admin123'; rotate it in .env.")
        if settings.VAULT_PIN == "1234":
            _startup_logger.warning("VAULT_PIN is still set to default '1234'; rotate it in Settings or .env.")
        if settings.QBITTORRENT_PASSWORD == "adminadmin":
            _startup_logger.warning("QBITTORRENT_PASSWORD is still set to the default value; rotate it in Settings or .env.")
        if settings.ALLOW_REGISTRATION and not settings.get_allowed_emails_set() and not (settings.REGISTRATION_INVITE_CODE or "").strip():
            _startup_logger.warning("ALLOW_REGISTRATION is enabled without ALLOWED_EMAILS or REGISTRATION_INVITE_CODE.")

    # 3b. Optional Sentry observability (if SENTRY_DSN configured in .env or EncryptedSetting)
    if _should_enable_sentry():
        init_sentry(settings.SENTRY_DSN)

    # 4. Initialize isolated HTTP/2 connection pools (search/AI vs telemetry/queue vs preview)
    search_client, telemetry_client = await init_http_clients()
    app.state.search_client = search_client
    app.state.telemetry_client = telemetry_client
    app.state.preview_client = get_preview_client()

    # 5. Ensure local SQLite FTS5 mirror schema & start background sync/prewarm loops
    await ensure_library_mirror_schema()
    sync_task = asyncio.create_task(run_library_sync_loop(interval_seconds=600))
    prewarm_task = asyncio.create_task(run_catalogue_prewarm_loop(interval_seconds=3600, top_n=15))
    is_test_run = "pytest" in sys.modules or os.getenv("ENVIRONMENT", "").strip().lower() == "test"
    watchdog_task = None if is_test_run else asyncio.create_task(run_watchdog_alert_loop(interval_seconds=1800))

    try:
        yield
    finally:
        sync_task.cancel()
        prewarm_task.cancel()
        bg_tasks = [sync_task, prewarm_task]
        if watchdog_task is not None:
            watchdog_task.cancel()
            bg_tasks.append(watchdog_task)
        await asyncio.gather(*bg_tasks, return_exceptions=True)
        await close_sftp_pool()
        await close_http_clients()


app = FastAPI(
    title="Unified Media Search & Seedbox Control Plane",
    description="Gemini-powered release assistant, unified search, and seedbox control plane.",
    version="1.0.0",
    default_response_class=ORJSONResponse,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
    lifespan=lifespan,
)

# CORS (restrictive HTTPS origins in production; localhost/HTTP only when DEBUG is enabled)
_cors_origins = [
    f"https://{settings.DOMAIN}",
    f"https://media.{settings.DOMAIN}",
]
if settings.DEBUG:
    _cors_origins.extend([
        "http://127.0.0.1:8888",
        "http://localhost:8888",
        f"http://{settings.DOMAIN}",
        f"http://media.{settings.DOMAIN}",
    ])

app.add_middleware(APIGuardMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# Register API Routers
app.include_router(auth.router)
app.include_router(auth.verify_router)
app.include_router(vault.router)
app.include_router(adult_preview.router)
app.include_router(search.suggest_router)
app.include_router(search.router)
app.include_router(grab.router)
app.include_router(queue.router)
app.include_router(library.router)
app.include_router(public_media.router)
app.include_router(stream.router)
app.include_router(stream.playback_router)
app.include_router(telemetry.router)
app.include_router(maintenance.router)
app.include_router(settings_router.router)
app.include_router(admin_users.router)


@app.get("/api/health", tags=["System"])
async def health_check():
    """Public, unauthenticated liveness probe (Docker HEALTHCHECK, scripts/deploy.sh,
    scripts/verify_domain.sh). Keep it minimal: never add hostnames, usernames, model
    names or other config here. Signed-in callers get those from /api/settings and
    /api/telemetry."""
    db_ok = True
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False
    return {
        "status": "healthy" if db_ok else "degraded",
        "db_ok": db_ok,
        "service": "media-search",
    }


# Static Files & SPA frontend
static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)

if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


async def _has_valid_active_session(request: Request, db: AsyncSession) -> bool:
    """Validate cookie signature, expiry, active user status, and token_version against DB."""
    token = request.cookies.get(settings.COOKIE_NAME)
    if not token:
        return False
    payload = decode_session_token(token)
    if not payload or not payload.get("sub"):
        return False

    stmt = select(User).where(User.username == payload["sub"])
    user = (await db.execute(stmt)).scalars().first()
    if not user or not user.is_active:
        return False

    db_ver = int(getattr(user, "token_version", 1) or 1)
    set_user_token_version(user.username, db_ver)
    return int(payload.get("ver", 1)) >= db_ver


@app.api_route("/media/hero-reel.mp4", methods=["GET", "HEAD"], tags=["Frontend"])
async def serve_hero_reel():
    """Serve runtime custom hero reel from DATA_DIR/media when present, else bundled reel."""
    override_file = Path(settings.DATA_DIR) / "media" / "hero-reel.mp4"
    target = override_file if override_file.is_file() else (static_dir / "media" / "hero-reel.mp4")
    if not target.is_file():
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(
        target,
        media_type="video/mp4",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.api_route("/media/hero-poster.jpg", methods=["GET", "HEAD"], tags=["Frontend"])
async def serve_hero_poster():
    """Serve runtime custom hero poster from DATA_DIR/media when present, else bundled poster."""
    override_file = Path(settings.DATA_DIR) / "media" / "hero-poster.jpg"
    target = override_file if override_file.is_file() else (static_dir / "media" / "hero-poster.jpg")
    if not target.is_file():
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return FileResponse(
        target,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Vary": "Cookie",
}


@app.api_route("/login", methods=["GET", "HEAD"], response_class=HTMLResponse, tags=["Frontend"])
@app.api_route("/register", methods=["GET", "HEAD"], response_class=HTMLResponse, tags=["Frontend"])
async def serve_login(request: Request, db: AsyncSession = Depends(get_db)):
    if await _has_valid_active_session(request, db):
        return RedirectResponse(url="/", status_code=status.HTTP_302_FOUND, headers=_NO_STORE_HEADERS)
    spa_index = static_dir / "app" / "index.html"
    if spa_index.exists():
        return FileResponse(spa_index, headers=_NO_STORE_HEADERS)
    login_file = static_dir / "login.html"
    if login_file.exists():
        return FileResponse(login_file, headers=_NO_STORE_HEADERS)
    return await serve_index()


@app.api_route("/reset-password", methods=["GET", "HEAD"], response_class=HTMLResponse, tags=["Frontend"])
async def serve_reset_password():
    spa_index = static_dir / "app" / "index.html"
    if spa_index.exists():
        return FileResponse(spa_index, headers=_NO_STORE_HEADERS)
    login_file = static_dir / "login.html"
    if login_file.exists():
        return FileResponse(login_file, headers=_NO_STORE_HEADERS)
    return await serve_index()


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse, tags=["Frontend"])
async def serve_index():
    spa_index = static_dir / "app" / "index.html"
    if spa_index.exists():
        return FileResponse(spa_index, headers={"Cache-Control": "no-cache"})
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(index_file, headers={"Cache-Control": "no-cache"})
    return HTMLResponse("<h1>Unified Media Search & Seedbox Control Plane API Running</h1>")
