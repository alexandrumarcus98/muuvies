import os
from pathlib import Path
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import select, text

from backend.app.config import settings
from backend.app.models import AuditLog, Base, PlaybackProgress, User, UserPasskey

# Ensure data folder exists
data_path = Path(settings.DATA_DIR)
data_path.mkdir(parents=True, exist_ok=True)

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    future=True,
)

from sqlalchemy import event as _sa_event


@_sa_event.listens_for(engine.sync_engine, "connect")
def _configure_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Apply high-concurrency SQLite pragmas on every physical connection."""
    if "sqlite" not in settings.DATABASE_URL.lower():
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON;")
    cursor.execute("PRAGMA journal_mode=WAL;")
    cursor.execute("PRAGMA synchronous=NORMAL;")
    cursor.execute("PRAGMA cache_size=-32000;")
    cursor.execute("PRAGMA temp_store=MEMORY;")
    cursor.execute("PRAGMA busy_timeout=5000;")
    cursor.close()


AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


async def run_sqlite_migrations(conn) -> None:
    """Apply numbered SQL migrations in backend/app/migrations/ tracked via PRAGMA user_version."""
    if "sqlite" not in settings.DATABASE_URL.lower():
        return
    migrations_dir = Path(__file__).resolve().parent / "migrations"
    if not migrations_dir.is_dir():
        return

    res = await conn.execute(text("PRAGMA user_version"))
    current_version = int(res.scalar() or 0)

    candidates = []
    for path in sorted(migrations_dir.glob("[0-9]*_*.sql")):
        prefix = path.name.split("_", 1)[0]
        if prefix.isdigit():
            candidates.append((int(prefix), path))
    candidates.sort(key=lambda item: item[0])

    for version, sql_path in candidates:
        if version <= current_version:
            continue
        raw_sql = sql_path.read_text(encoding="utf-8")
        for stmt in raw_sql.split(";"):
            cleaned = "\n".join(
                line for line in stmt.splitlines() if not line.strip().startswith("--")
            ).strip()
            if cleaned:
                await conn.execute(text(cleaned))
        await conn.execute(text(f"PRAGMA user_version = {version}"))
        current_version = version


async def init_db() -> None:
    # Import security helpers here to avoid circular dependencies
    from backend.app.security import hash_secret
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Auto-migrate SQLite users table if missing new columns
        try:
            res = await conn.execute(text("PRAGMA table_info(users)"))
            cols = {row[1] for row in res.fetchall()}
            if cols:
                if "email" not in cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(128)"))
                if "role" not in cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(32) DEFAULT 'user'"))
                if "is_active" not in cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN is_active BOOLEAN DEFAULT 1"))
                if "token_version" not in cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN token_version INTEGER DEFAULT 1"))
                if "email_verified_at" not in cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN email_verified_at DATETIME"))
                    await conn.execute(text("UPDATE users SET email_verified_at = CURRENT_TIMESTAMP WHERE email_verified_at IS NULL"))
                if "verification_token_hash" not in cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN verification_token_hash VARCHAR(128)"))
                if "verification_expires_at" not in cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN verification_expires_at DATETIME"))
                if "reset_token_hash" not in cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN reset_token_hash VARCHAR(128)"))
                if "reset_expires_at" not in cols:
                    await conn.execute(text("ALTER TABLE users ADD COLUMN reset_expires_at DATETIME"))
        except Exception:
            pass
        await run_sqlite_migrations(conn)

    # Ensure admin user has role and email
    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.username == settings.ADMIN_USERNAME)
        result = await session.execute(stmt)
        user = result.scalars().first()

        admin_email = (settings.ADMIN_EMAIL or "").strip() or f"admin@{settings.DOMAIN}"
        if not user:
            from backend.app.models import utcnow
            pw_hash = hash_secret(settings.ADMIN_PASSWORD)
            pin_hash = hash_secret(settings.VAULT_PIN)
            admin = User(
                username=settings.ADMIN_USERNAME,
                email=admin_email,
                password_hash=pw_hash,
                role="admin",
                is_active=True,
                vault_pin_hash=pin_hash,
                email_verified_at=utcnow(),
            )
            session.add(admin)
            await session.commit()
        else:
            updated = False
            if getattr(user, "email_verified_at", None) is None:
                from backend.app.models import utcnow
                user.email_verified_at = utcnow()
                updated = True
            if not getattr(user, "email", None):
                user.email = admin_email
                updated = True
            if getattr(user, "role", None) != "admin":
                user.role = "admin"
                updated = True
            if not getattr(user, "is_active", True):
                user.is_active = True
                updated = True
            from backend.app.security import verify_secret
            if not verify_secret(settings.ADMIN_PASSWORD, user.password_hash):
                user.password_hash = hash_secret(settings.ADMIN_PASSWORD)
                updated = True
            if not (user.vault_pin_hash or "").strip():
                user.vault_pin_hash = hash_secret(settings.VAULT_PIN)
                updated = True
            if updated:
                await session.commit()

