"""Adult Vault (/api/vault/x/preview/*) routes for pre-download release previews."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Response, status
from fastapi.responses import ORJSONResponse

from backend.app.routers._access import vault_router
from backend.app.schemas import AdultPreviewImage, AdultPreviewResponse
from backend.app.security import require_vault_unlocked
from backend.app.services.adult_preview import (
    PreviewBlockedError,
    PreviewInvalidImageError,
    PreviewNotFoundError,
    PreviewRateLimitError,
    PreviewTooLargeError,
    PreviewUnsupportedMediaError,
    PreviewUpstreamError,
    fetch_preview_image,
    resolve_preview_images,
)
from backend.app.services.streaming import (
    PREVIEW_IMAGE_TOKEN_TTL_SECONDS,
    create_preview_token,
    verify_preview_token,
)

router = vault_router(prefix="/api/vault/x/preview", tags=["Adult Vault Previews"])

_PREVIEW_HEADERS = {
    "Cache-Control": "no-store, private",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Disposition": "inline",
}


def _map_preview_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (PreviewNotFoundError, PreviewBlockedError)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    if isinstance(exc, PreviewRateLimitError):
        return HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    if isinstance(exc, PreviewTooLargeError):
        return HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Preview image exceeds 2 MB limit")
    if isinstance(exc, PreviewUnsupportedMediaError):
        return HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Unsupported image format")
    if isinstance(exc, PreviewInvalidImageError):
        return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Image too small")
    if isinstance(exc, PreviewUpstreamError):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Preview source unavailable")
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Preview source unavailable")


@router.get("/img/{img_token}")
async def get_adult_preview_image(
    img_token: str,
    session: dict = Depends(require_vault_unlocked),
):
    """Fetch, magic-verify, and proxy one preview image inside the unlocked Adult Vault."""
    payload = verify_preview_token(img_token, expected_kind="adult-preview-img")
    if not payload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    tok_sub = str(payload.get("sub") or "")
    cur_sub = str(session.get("sub") or "")
    if tok_sub and tok_sub != cur_sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    img_url = str(payload.get("url") or "").strip()
    indexer = str(payload.get("indexer") or "Unknown")
    if not img_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    try:
        data, mime = await fetch_preview_image(
            indexer=indexer,
            img_url=img_url,
            session_key=cur_sub,
        )
    except Exception as exc:
        raise _map_preview_error(exc) from exc

    return Response(content=data, media_type=mime, headers=_PREVIEW_HEADERS)


@router.get("/{token}", response_model=AdultPreviewResponse)
async def get_adult_preview_images(
    token: str,
    session: dict = Depends(require_vault_unlocked),
):
    """Resolve up to 5 cover/thumbnail image tokens for an unlocked adult search result."""
    payload = verify_preview_token(token, expected_kind="adult-preview")
    if not payload:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    tok_sub = str(payload.get("sub") or "")
    cur_sub = str(session.get("sub") or "")
    if tok_sub and tok_sub != cur_sub:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    indexer = str(payload.get("indexer") or "")
    info_url = payload.get("info_url")
    poster_url = payload.get("poster_url")

    try:
        images = await resolve_preview_images(
            indexer=indexer,
            info_url=info_url,
            poster_url=poster_url,
            session_key=cur_sub,
        )
    except Exception as exc:
        raise _map_preview_error(exc) from exc

    vault_exp = int(session.get("vault_exp", 0) or 0)
    out_images = [
        AdultPreviewImage(
            id=create_preview_token(
                {
                    "indexer": indexer,
                    "url": img["url"],
                    "sub": cur_sub,
                },
                vault_exp=vault_exp,
                ttl_seconds=PREVIEW_IMAGE_TOKEN_TTL_SECONDS,
                kind="adult-preview-img",
            ),
            role=img["role"],
        )
        for img in images
    ]
    res_body = AdultPreviewResponse(images=out_images, source=indexer)
    return ORJSONResponse(content=res_body.model_dump(), headers=_PREVIEW_HEADERS)

