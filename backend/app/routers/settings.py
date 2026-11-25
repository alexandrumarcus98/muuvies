from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models import EncryptedSetting, User
from backend.app.observability import init_sentry
from backend.app.routers._access import admin_router
from backend.app.schemas import SettingsResponse, SettingsUpdateRequest
from backend.app.security import (
    require_admin,
    encrypt_text,
    decrypt_text,
    hash_secret,
)
from backend.app.services.prowlarr import prowlarr_client
from backend.app.services.qbittorrent import qb_client

router = admin_router(prefix="/api/settings", tags=["Settings & Credentials"])


@router.get("", response_model=SettingsResponse)
async def get_settings_status(
    session: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    # Fetch runtime overrides from encrypted_settings
    stmt = select(EncryptedSetting)
    res = await db.execute(stmt)
    overrides = {s.key: True for s in res.scalars().all()}

    return SettingsResponse(
        ai_provider=settings.AI_PROVIDER,
        gemini_api_key_configured=bool(settings.GEMINI_API_KEY) or overrides.get("gemini_api_key", False),
        gemini_model=settings.GEMINI_MODEL,
        github_token_configured=bool(settings.GITHUB_TOKEN) or overrides.get("github_token", False),
        github_model=settings.GITHUB_MODEL,
        seedbox_host=settings.SEEDBOX_HOST,
        seedbox_user=settings.SEEDBOX_USER,
        qbittorrent_configured=bool(settings.QBITTORRENT_URL),
        prowlarr_configured=bool(settings.PROWLARR_API_KEY) or overrides.get("prowlarr_api_key", False),
        radarr_configured=bool(settings.RADARR_API_KEY) or overrides.get("radarr_api_key", False),
        sonarr_configured=bool(settings.SONARR_API_KEY) or overrides.get("sonarr_api_key", False),
        whisparr_configured=bool(settings.WHISPARR_API_KEY) or overrides.get("whisparr_api_key", False),
        cross_seed_configured=bool(settings.CROSS_SEED_URL),
        vault_pin_configured=True,
        sentry_configured=bool(settings.SENTRY_DSN) or overrides.get("sentry_dsn", False),
    )


@router.post("")
async def update_settings(
    payload: SettingsUpdateRequest,
    session: dict = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    updated = []

    async def save_override(key: str, val: str):
        enc = encrypt_text(val)
        existing = await db.get(EncryptedSetting, key)
        if existing:
            existing.encrypted_value = enc
        else:
            db.add(EncryptedSetting(key=key, encrypted_value=enc))
        updated.append(key)

    if payload.gemini_api_key:
        await save_override("gemini_api_key", payload.gemini_api_key)
        settings.GEMINI_API_KEY = payload.gemini_api_key

    if payload.github_token:
        await save_override("github_token", payload.github_token)
        settings.GITHUB_TOKEN = payload.github_token

    if payload.ai_provider:
        await save_override("ai_provider", payload.ai_provider)
        settings.AI_PROVIDER = payload.ai_provider

    if payload.prowlarr_api_key:
        await save_override("prowlarr_api_key", payload.prowlarr_api_key)
        settings.PROWLARR_API_KEY = payload.prowlarr_api_key
        prowlarr_client._indexer_cache = None

    if payload.radarr_api_key:
        await save_override("radarr_api_key", payload.radarr_api_key)
        settings.RADARR_API_KEY = payload.radarr_api_key

    if payload.sonarr_api_key:
        await save_override("sonarr_api_key", payload.sonarr_api_key)
        settings.SONARR_API_KEY = payload.sonarr_api_key

    if payload.whisparr_api_key:
        await save_override("whisparr_api_key", payload.whisparr_api_key)
        settings.WHISPARR_API_KEY = payload.whisparr_api_key

    if payload.cross_seed_api_key:
        await save_override("cross_seed_api_key", payload.cross_seed_api_key)
        settings.CROSS_SEED_API_KEY = payload.cross_seed_api_key

    if payload.qbittorrent_password:
        await save_override("qbittorrent_password", payload.qbittorrent_password)
        settings.QBITTORRENT_PASSWORD = payload.qbittorrent_password
        qb_client._cookie = None

    if payload.vault_pin and payload.vault_pin.strip():
        clean_pin = payload.vault_pin.strip()
        await save_override("vault_pin", clean_pin)
        username = session.get("sub", settings.ADMIN_USERNAME)
        stmt = select(User).where(User.username == username)
        res = await db.execute(stmt)
        user = res.scalars().first()
        if user:
            user.vault_pin_hash = hash_secret(clean_pin)
        settings.VAULT_PIN = clean_pin

    if payload.sentry_dsn is not None:
        cleaned_dsn = payload.sentry_dsn.strip()
        if cleaned_dsn:
            await save_override("sentry_dsn", cleaned_dsn)
            settings.SENTRY_DSN = cleaned_dsn
            init_sentry(cleaned_dsn)
        else:
            existing_dsn = await db.get(EncryptedSetting, "sentry_dsn")
            if existing_dsn:
                await db.delete(existing_dsn)
            settings.SENTRY_DSN = ""
            updated.append("sentry_dsn")

    await db.commit()
    return {"success": True, "updated": updated}

