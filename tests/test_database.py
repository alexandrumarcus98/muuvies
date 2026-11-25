import pytest
import uuid
from sqlalchemy import select

from backend.app.database import AsyncSessionLocal, init_db
from backend.app.models import GrabHistory, SearchLog, EncryptedSetting
from backend.app.security import encrypt_text, decrypt_text


@pytest.mark.asyncio
async def test_database_models_and_encryption():
    await init_db()

    test_uid = uuid.uuid4().hex[:8]
    test_hash = f"hash_{test_uid}"
    test_key = f"key_{test_uid}"

    async with AsyncSessionLocal() as session:
        # 1. Grab History
        item = GrabHistory(
            info_hash=test_hash,
            title=f"Dune.Part.Two.{test_uid}.2160p.UHD.Remux",
            category="Movies",
            indexer="PrivateMovies",
            size=65000000000,
            target="qbittorrent",
            status="grabbed",
        )
        session.add(item)

        # 2. Search Log
        slog = SearchLog(
            query=f"Dune Part Two {test_uid}",
            category="Movies",
            parsed_filters='{"resolution": "2160p"}',
            ai_used=True,
            result_count=12,
        )
        session.add(slog)

        # 3. Encrypted Setting
        enc_val = encrypt_text("super-secret-api-key")
        setting = EncryptedSetting(
            key=test_key,
            encrypted_value=enc_val,
        )
        session.add(setting)

        await session.commit()

    # Query back
    async with AsyncSessionLocal() as session:
        # Verify GrabHistory
        res = await session.execute(
            select(GrabHistory).where(GrabHistory.info_hash == test_hash)
        )
        found_grab = res.scalars().first()
        assert found_grab is not None
        assert found_grab.info_hash == test_hash
        assert found_grab.category == "Movies"

        # Verify EncryptedSetting
        res_setting = await session.execute(
            select(EncryptedSetting).where(EncryptedSetting.key == test_key)
        )
        found_setting = res_setting.scalars().first()
        assert found_setting is not None
        assert decrypt_text(found_setting.encrypted_value) == "super-secret-api-key"
