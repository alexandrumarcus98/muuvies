import atexit
import os
import tempfile

from cryptography.fernet import Fernet

# Isolate pytest runs onto a dedicated temporary directory & SQLite database before any backend imports.
_TEST_TMP_DIR = tempfile.TemporaryDirectory(prefix="muuvies_pytest_")
atexit.register(_TEST_TMP_DIR.cleanup)

os.environ["DATA_DIR"] = _TEST_TMP_DIR.name
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_TMP_DIR.name}/test_media_search.db"
os.environ["FERNET_KEY"] = Fernet.generate_key().decode("utf-8")

# Disable production Sentry telemetry and email verification gating during pytest runs.
os.environ["SENTRY_DSN"] = ""
os.environ["ENVIRONMENT"] = "test"
os.environ["ALLOWED_EMAILS"] = ""
os.environ["ALLOW_REGISTRATION"] = "true"
os.environ["RESEND_API_KEY"] = ""
os.environ["REQUIRE_EMAIL_VERIFICATION"] = "false"
os.environ["COOKIE_SECURE"] = "false"
os.environ["DOMAIN"] = "example.com"
os.environ["PUBLIC_BASE_URL"] = "https://media.example.com"
os.environ["PROWLARR_URL"] = "http://127.0.0.1:9696"
os.environ["QBITTORRENT_URL"] = "http://127.0.0.1:8080"
os.environ["WHISPARR_URL"] = "http://127.0.0.1:6969"
os.environ["PRIVATE_TRACKERS"] = ""
os.environ["ADULT_INDEXER_KEYWORDS"] = ""
os.environ["ADULT_PREVIEW_SOURCES_JSON"] = ""
os.environ["ADULT_PREVIEW_IMAGE_HOSTS"] = ""
os.environ["ADULT_PREVIEW_IMAGE_SUBDOMAIN_HOSTS"] = ""
os.environ.setdefault("SEEDBOX_HOST", "seedbox.example.invalid")
os.environ.setdefault("SEEDBOX_USER", "seedboxuser")

