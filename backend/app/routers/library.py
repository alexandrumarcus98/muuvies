from fastapi import Depends

from backend.app.routers._access import user_router
from backend.app.security import get_current_session, check_vault_unlocked
from backend.app.services.library_mirror import get_library_overview_from_mirror

router = user_router(prefix="/api/library", tags=["Media Library"])


@router.get("")
async def get_library_overview(
    session: dict = Depends(get_current_session),
):
    """Serve *arr library inventory from local SQLite mirror (<2ms)."""
    vault_unlocked = check_vault_unlocked(session)
    return await get_library_overview_from_mirror(vault_unlocked=vault_unlocked)
