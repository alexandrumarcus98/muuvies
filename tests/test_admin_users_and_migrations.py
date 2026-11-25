import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from backend.app.config import settings
from backend.app.database import AsyncSessionLocal, engine, init_db
from backend.app.main import app
from backend.app.models import User
from backend.app.security import create_session_token, hash_secret


@pytest.mark.asyncio
async def test_sqlite_migrations_set_user_version():
    await init_db()
    async with engine.connect() as conn:
        ver = int((await conn.execute(text("PRAGMA user_version"))).scalar() or 0)
        assert ver >= 1


@pytest.mark.asyncio
async def test_admin_users_list_and_role_update():
    await init_db()
    async with AsyncSessionLocal() as db:
        admin = (await db.execute(select(User).where(User.username == settings.ADMIN_USERNAME))).scalars().first()
        assert admin is not None

        std = (await db.execute(select(User).where(User.username == "alice_admin_test"))).scalars().first()
        if not std:
            std = User(
                username="alice_admin_test",
                email="alice_admin_test@example.com",
                password_hash=hash_secret("pass1234"),
                role="user",
                is_active=True,
                token_version=1,
            )
            db.add(std)
            await db.commit()
            await db.refresh(std)
        std_id = int(std.id)

    admin_tok = create_session_token(settings.ADMIN_USERNAME, role="admin", ver=int(admin.token_version or 1))
    user_tok = create_session_token("alice_admin_test", role="user", ver=1)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Non-admin must be rejected
        r_forbid = await client.get("/api/admin/users", cookies={settings.COOKIE_NAME: user_tok})
        assert r_forbid.status_code == 403

        # Admin lists users
        r_list = await client.get("/api/admin/users", cookies={settings.COOKIE_NAME: admin_tok})
        assert r_list.status_code == 200
        users = r_list.json()["users"]
        assert any(u["username"] == "alice_admin_test" for u in users)

        # Admin promotes & revokes sessions
        r_patch = await client.patch(
            f"/api/admin/users/{std_id}",
            json={"role": "admin", "revoke_sessions": True},
            cookies={settings.COOKIE_NAME: admin_tok},
        )
        assert r_patch.status_code == 200
        updated = r_patch.json()
        assert updated["role"] == "admin"
        assert updated["token_version"] >= 2

        # Cannot demote self
        r_self = await client.patch(
            f"/api/admin/users/{int(admin.id)}",
            json={"role": "user"},
            cookies={settings.COOKIE_NAME: admin_tok},
        )
        assert r_self.status_code == 400

