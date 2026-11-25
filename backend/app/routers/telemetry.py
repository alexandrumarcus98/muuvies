from fastapi import Depends
from backend.app.routers._access import user_router
from backend.app.schemas import TelemetryResponse
from backend.app.security import get_current_session, check_vault_unlocked
from backend.app.services.telemetry import get_live_telemetry

router = user_router(prefix="/api/telemetry", tags=["Live Seedbox Telemetry"])


@router.get("", response_model=TelemetryResponse)
async def get_telemetry_status(
    session: dict = Depends(get_current_session),
):
    vault_unlocked = check_vault_unlocked(session)
    telemetry = await get_live_telemetry(
        vault_unlocked=vault_unlocked,
        include_infra=session.get("role") == "admin",
    )
    return telemetry

