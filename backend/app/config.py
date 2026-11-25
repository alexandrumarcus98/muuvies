import os
import secrets
from pathlib import Path
from urllib.parse import urlparse
from cryptography.fernet import Fernet
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # App
    HOST: str = "127.0.0.1"
    PORT: int = 8888
    DEBUG: bool = False
    SECRET_KEY: str = "dev-secret-key-change-in-production-1234567890"
    FERNET_KEY: str = ""
    DOMAIN: str = "example.com"
    PUBLIC_BASE_URL: str = ""
    WEBAUTHN_RP_ID: str = ""
    WEBAUTHN_RP_NAME: str = "Muuvies"
    ALLOW_REGISTRATION: bool = False
    REGISTRATION_INVITE_CODE: str = ""
    ALLOWED_EMAILS: str = ""
    ADMIN_EMAIL: str = ""

    # Email Verification (Resend REST API)
    RESEND_API_KEY: str = ""
    EMAIL_FROM: str = "Muuvies <noreply@example.com>"
    REQUIRE_EMAIL_VERIFICATION: bool = False

    # Auth & Adult Vault
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "admin123"
    VAULT_PIN: str = "1234"
    SESSION_EXPIRE_HOURS: int = 24
    VAULT_TIMEOUT_MINUTES: int = 30
    COOKIE_SECURE: bool = False
    COOKIE_NAME: str = "media_session"

    # AI Providers (Google Gemini, Azure OpenAI, & GitHub Models)
    AI_PROVIDER: str = "auto"  # auto, gemini, azure, github, none
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"

    AZURE_OPENAI_API_KEY: str = ""
    AZURE_OPENAI_ENDPOINT: str = ""
    AZURE_OPENAI_DEPLOYMENT: str = "gpt-5-mini"

    GITHUB_TOKEN: str = ""
    GITHUB_MODEL: str = "gpt-4o-mini"
    GITHUB_MODELS_URL: str = "https://models.inference.ai.azure.com/chat/completions"

    # Seedbox & Tunnel info (configured via .env)
    SEEDBOX_HOST: str = ""
    SEEDBOX_USER: str = ""
    SEEDBOX_SSH_PORT: int = 22

    # Service Endpoints
    QBITTORRENT_URL: str = "http://127.0.0.1:8080"
    QBITTORRENT_USERNAME: str = "admin"
    QBITTORRENT_PASSWORD: str = "adminadmin"

    PROWLARR_URL: str = "http://127.0.0.1:9696"
    PROWLARR_API_KEY: str = ""

    RADARR_URL: str = "http://127.0.0.1:7878"
    RADARR_API_KEY: str = ""
    RADARR_ROOT_FOLDER: str = ""

    SONARR_URL: str = "http://127.0.0.1:8989"
    SONARR_API_KEY: str = ""
    SONARR_ROOT_FOLDER: str = ""

    WHISPARR_URL: str = "http://127.0.0.1:6969"
    WHISPARR_API_KEY: str = ""
    WHISPARR_ROOT_FOLDER: str = ""

    # Tracker & Adult Vault Customization (configured via .env)
    PRIVATE_TRACKERS: str = ""
    ADULT_INDEXER_KEYWORDS: str = ""
    ADULT_PREVIEW_SOURCES_JSON: str = ""
    ADULT_PREVIEW_IMAGE_HOSTS: str = ""
    ADULT_PREVIEW_IMAGE_SUBDOMAIN_HOSTS: str = ""

    CROSS_SEED_URL: str = "http://127.0.0.1:2468"
    CROSS_SEED_API_KEY: str = ""

    # 4K Theater Streaming (Jellyfin HLS remux + optional mounted media root)
    JELLYFIN_URL: str = ""
    JELLYFIN_API_KEY: str = ""
    STREAM_MEDIA_ROOT: str = ""

    # Storage
    DATA_DIR: str = "data"
    DATABASE_URL: str = "sqlite+aiosqlite:///data/media_search.db"

    # Optional Observability (Sentry)
    SENTRY_DSN: str = ""

    # Public Landing Showcase (TMDB Read-Only Token or v3 Key)
    TMDB_READ_ONLY_API_KEY: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def _auto_enable_email_verification(self) -> "Settings":
        if (
            "REQUIRE_EMAIL_VERIFICATION" not in self.model_fields_set
            and (self.RESEND_API_KEY or "").strip()
        ):
            self.REQUIRE_EMAIL_VERIFICATION = True
        return self

    def get_allowed_emails_set(self) -> set[str]:
        return {
            e.strip().lower()
            for e in (self.ALLOWED_EMAILS or "").split(",")
            if e.strip()
        }

    def get_webauthn_rp_id(self, request_host: str = "") -> str:
        """Resolve WebAuthn RP ID from WEBAUTHN_RP_ID, dev/test host, or PUBLIC_BASE_URL."""
        if (self.WEBAUTHN_RP_ID or "").strip():
            return self.WEBAUTHN_RP_ID.strip().lower()
        clean_host = (request_host or "").split(":")[0].strip().lower()
        if clean_host in {"localhost", "127.0.0.1", "test"}:
            return clean_host
        base = (self.PUBLIC_BASE_URL or "").strip()
        if base:
            parsed = urlparse(base if "://" in base else f"https://{base}")
            if parsed.hostname:
                return parsed.hostname.lower()
        return clean_host or "localhost"

    def get_fernet_key(self) -> bytes:
        if self.FERNET_KEY:
            try:
                k = self.FERNET_KEY.encode("utf-8")
                Fernet(k)  # test validity
                return k
            except Exception:
                pass
        # Fall back to a stable key file in DATA_DIR or generate one
        key_file = Path(self.DATA_DIR) / ".fernet_key"
        if key_file.exists():
            return key_file.read_bytes().strip()
        new_key = Fernet.generate_key()
        try:
            Path(self.DATA_DIR).mkdir(parents=True, exist_ok=True)
            key_file.write_bytes(new_key)
        except Exception:
            pass
        return new_key


settings = Settings()

