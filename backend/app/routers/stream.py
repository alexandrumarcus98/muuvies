import asyncio
import logging
import posixpath
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlparse

import httpx
from fastapi import Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.ai_assistant import parse_release_attributes
from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models import PlaybackProgress, utcnow
from backend.app.routers._access import public_router, user_router
from backend.app.routers.auth import _require_active_db_user
from backend.app.schemas import PlaybackProgressResponse, PlaybackProgressUpdate
from backend.app.security import (
    check_vault_unlocked,
    get_current_session,
    get_current_user_optional,
    is_trusted_proxy_ip,
)
from backend.app.services.http_clients import get_search_client
from backend.app.services.qbittorrent import qb_client
from backend.app.services.torrent_stream import (
    available_end,
    contiguous_fraction,
    file_offset,
    normalize_info_hash,
    pick_stream_file,
)
from backend.app.services.streaming import (
    CHUNK_SIZE,
    build_proxy_hls_url,
    create_stream_token,
    guess_video_mime_type,
    iter_file_range,
    jellyfin_auth_headers,
    open_sftp_range_stream,
    parse_range_header,
    resolve_arr_item_file_path,
    resolve_jellyfin_id,
    resolve_local_media_path,
    resolve_media_item,
    rewrite_m3u8_playlist,
    verify_stream_token,
)

logger = logging.getLogger("muuvies.stream")
router = public_router(prefix="/api/stream", tags=["4K Theater Streaming"])
_user_router = user_router(tags=["4K Theater Streaming"])
playback_router = user_router(prefix="/api/playback", tags=["Playback Progress"])


_HLS_SUBPATH_RE = re.compile(r"^[A-Za-z0-9_./-]+\.(?:m3u8|ts|mp4|m4s|vtt|aac|ac3)$")
_ALLOWED_HLS_QUERY_KEYS = frozenset({
    "videocodec",
    "audiocodec",
    "allowvideostreamcopy",
    "allowaudiostreamcopy",
    "transcodingmaxaudiochannels",
    "maxstreamingbitrate",
    "mediasourceid",
    "playsessionid",
    "segmentcontainer",
    "breakonnonkeyframes",
    "transcodereasons",
    "subtitlemethod",
    "subtitlecodec",
    "segmentid",
    "runtimeticks",
    "actualsegmentlengthTicks".lower(),
    "videobitrate",
    "audiobitrate",
    "audiostreamindex",
    "subtitlestreamindex",
    "deviceid",
    "static",
    "tag",
    "livestreamid",
})


def _public_base_url(request: Request) -> str:
    """Resolve externally reachable base URL, preferring PUBLIC_BASE_URL / DOMAIN over untrusted headers."""
    pub = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    if pub:
        return pub

    peer_ip = request.client.host if request.client else ""
    trusted = is_trusted_proxy_ip(peer_ip)
    fwd_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower() if trusted else ""
    fwd_host = (
        (request.headers.get("x-forwarded-host") or request.headers.get("host") or "")
        if trusted
        else (request.headers.get("host") or "")
    ).split(",")[0].strip()

    host_name = fwd_host.split(":")[0].strip().lower()
    domain = (settings.DOMAIN or "").strip().lower()
    allowed_hosts = {"localhost", "127.0.0.1", "test"}
    if domain:
        allowed_hosts.update({domain, f"media.{domain}"})

    if trusted and fwd_proto in ("http", "https") and fwd_host and host_name in allowed_hosts:
        return f"{fwd_proto}://{fwd_host}"

    if domain and host_name not in {"localhost", "127.0.0.1", "test"}:
        return f"https://media.{domain}"
    return str(request.base_url).rstrip("/")


def _is_adult_item_id(item_id: str) -> bool:
    clean = (item_id or "").strip().lower()
    return clean.startswith("whisparr:")


def _is_adult_category(category: Optional[str]) -> bool:
    cat = (category or "").strip().lower()
    return cat == "adult" or "xxx" in cat


async def _safe_qb_hashes() -> set:
    """Hashes of qBittorrent torrents that are known NOT to be adult.

    Playback rows for `qb:<hash>` don't record a category, so while the vault is
    locked a torrent row is shown only if qBittorrent still has it in a
    non-adult category. Anything unknown (removed torrent, qB unreachable) is
    treated as adult and hidden.
    """
    try:
        rows = await qb_client.get_queue(filter_status="all")
    except Exception:
        return set()
    return {str(t.get("hash", "")).lower() for t in rows if not _is_adult_category(t.get("category"))}


async def _hidden_while_locked(item_id: str, safe_qb: Optional[set] = None) -> bool:
    clean = (item_id or "").strip().lower()
    if clean.startswith("whisparr:"):
        return True
    if clean.startswith("qb:"):
        if safe_qb is None:
            safe_qb = await _safe_qb_hashes()
        return clean[3:] not in safe_qb
    return False


def _serialize_progress(row: PlaybackProgress) -> PlaybackProgressResponse:
    pos = float(row.position_seconds or 0.0)
    dur = float(row.duration_seconds or 0.0)
    pct = round(min(100.0, (pos / dur) * 100.0), 1) if dur > 0 else 0.0
    return PlaybackProgressResponse(
        item_id=row.item_id,
        title=row.title or "",
        position_seconds=round(pos, 2),
        duration_seconds=round(dur, 2),
        progress_pct=pct,
        completed=bool(row.completed),
        updated_at=row.updated_at,
    )


async def _require_stream_token(token: str, request: Request) -> Dict[str, Any]:
    payload = verify_stream_token(token)
    if not payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired stream token")
    if payload.get("is_adult"):
        session = await get_current_user_optional(request)
        if not session or not check_vault_unlocked(session):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media item not found")
    return payload


# ---------------------------------------------------------------------------
# Watching a torrent while qBittorrent is still downloading it
# ---------------------------------------------------------------------------

_QB_WINDOW_TTL = 2.0
_QB_WAIT_SECONDS = 20.0
_qb_window_cache: Dict[str, Any] = {}


async def _qb_stream_window(qb_hash: str, fresh: bool = False) -> Optional[Dict[str, Any]]:
    """Where a torrent's main video lives and which of its pieces are on disk.

    Cached for 2 s per torrent: a player fires many range requests per second.
    Returns None while the torrent is unknown or has no file list yet (magnet
    still fetching metadata).
    """
    now = time.monotonic()
    cached = _qb_window_cache.get(qb_hash)
    if cached and not fresh and now - cached["at"] < _QB_WINDOW_TTL:
        return cached["window"]

    info = await qb_client.get_torrent(qb_hash)
    files = await qb_client.get_torrent_files(qb_hash) if info else []
    picked = pick_stream_file(files)
    window: Optional[Dict[str, Any]] = None
    if info and picked:
        pos, entry = picked
        window = {
            "path": posixpath.join(str(info.get("save_path") or ""), str(entry.get("name") or "")),
            "size": int(entry.get("size") or 0),
            "progress": float(info.get("progress") or 0.0),
            "streaming": bool(info.get("seq_dl")) and bool(info.get("f_l_piece_prio")),
            "complete": float(entry.get("progress") or 0.0) >= 1.0,
        }
        if not window["complete"]:
            props = await qb_client.get_torrent_properties(qb_hash)
            window["piece_size"] = int(props.get("piece_size") or 0)
            window["offset"] = file_offset(files, pos)
            window["states"] = await qb_client.get_piece_states(qb_hash)
            window["playable"] = contiguous_fraction(
                window["size"], window["offset"], window["piece_size"], window["states"]
            )
        else:
            window["playable"] = 1.0
    _qb_window_cache[qb_hash] = {"at": now, "window": window}
    if len(_qb_window_cache) > 64:
        oldest = min(_qb_window_cache, key=lambda k: _qb_window_cache[k]["at"])
        _qb_window_cache.pop(oldest, None)
    return window


async def _clamp_to_downloaded(qb_hash: str, range_hdr: Optional[str]) -> Optional[tuple]:
    """For an unfinished torrent, turn the request into a range that's on disk.

    Returns (exact remote file path, Range header to use), or None when the file
    is complete and needs no clamping. Waits up to 20 s for the first requested
    piece (sequential mode fetches it next), then answers 503 + Retry-After.
    """
    window = await _qb_stream_window(qb_hash)
    if window is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Waiting for the torrent to start",
                            headers={"Retry-After": "3"})
    if window["complete"]:
        return window["path"], range_hdr
    size = window["size"]
    start, end = parse_range_header(range_hdr or "bytes=0-", size)
    deadline = time.monotonic() + _QB_WAIT_SECONDS
    while True:
        last = available_end(start, end, window["offset"], window["piece_size"], window["states"])
        if last is not None:
            return window["path"], f"bytes={start}-{last}"
        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="This part of the video is still downloading",
                headers={"Retry-After": "3"},
            )
        await asyncio.sleep(1.0)
        window = await _qb_stream_window(qb_hash, fresh=True) or window
        if window["complete"]:
            return window["path"], range_hdr


@_user_router.post("/qb/{torrent_hash}/stream-mode")
async def enable_qb_stream_mode(
    torrent_hash: str,
    session: dict = Depends(get_current_session),
):
    """Switch one torrent to sequential download so it can be watched now."""
    qb_hash = normalize_info_hash(torrent_hash)
    if not qb_hash:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media item not found")
    # Same visibility rules as playback: adult torrents are 404 while the vault is locked.
    await resolve_media_item(f"qb:{qb_hash}", vault_unlocked=check_vault_unlocked(session))
    info = await qb_client.enable_stream_mode(qb_hash)
    if info is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media item not found")
    _qb_window_cache.pop(qb_hash, None)
    return {
        "hash": qb_hash,
        "streaming": bool(info.get("seq_dl")) and bool(info.get("f_l_piece_prio")),
        "progress": round(float(info.get("progress") or 0.0) * 100.0, 1),
    }


@_user_router.get("/{item_id}/manifest")
async def get_stream_manifest(
    item_id: str,
    request: Request,
    title: Optional[str] = Query(None),
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    vault_unlocked = check_vault_unlocked(session)
    media = await resolve_media_item(item_id, vault_unlocked=vault_unlocked, title_hint=title)
    attrs = parse_release_attributes(media["title"])
    hdr = "Dolby Vision" if attrs["has_dv"] else "HDR10+" if "HDR10+" in attrs["tags"] else "HDR10" if attrs["has_hdr"] else "SDR"

    qb_hash = normalize_info_hash(media["id"][3:]) if media["id"].startswith("qb:") else None
    qb_window = await _qb_stream_window(qb_hash, fresh=True) if qb_hash else None
    if qb_hash and qb_window is None:
        # Just grabbed: magnet metadata or the file list isn't there yet.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Waiting for the torrent to start")
    downloading = bool(qb_window and not qb_window["complete"])

    if qb_window:
        file_path = qb_window["path"]
    else:
        file_path = await resolve_arr_item_file_path(media["id"], media.get("file_path") or "")
    if downloading:
        # Jellyfin doesn't know a file that hasn't been imported yet: direct play only.
        jf_id, hls_configured = "", False
    else:
        jf_id, hls_configured = await resolve_jellyfin_id(media["title"], media["id"], file_path=file_path)
    resolved_jf_id = jf_id if hls_configured else ""
    if not file_path and not resolved_jf_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file path not available on stream proxy")

    resume_seconds = 0.0
    duration_seconds = 0.0
    try:
        user = await _require_active_db_user(session, db)
        prog_row = (
            await db.execute(
                select(PlaybackProgress).where(
                    PlaybackProgress.user_id == user.id,
                    PlaybackProgress.item_id == media["id"],
                )
            )
        ).scalars().first()
        if prog_row:
            resume_seconds = round(float(prog_row.position_seconds or 0.0), 2)
            duration_seconds = round(float(prog_row.duration_seconds or 0.0), 2)
    except HTTPException:
        raise
    except Exception:
        pass

    token = create_stream_token({
        "item_id": media["id"],
        "title": media["title"],
        "is_adult": media["is_adult"],
        "file_path": file_path,
        "jf_id": resolved_jf_id,
        "qb_hash": qb_hash or "",
    })
    direct_path = f"/api/stream/file/{token}"
    full_direct = f"{_public_base_url(request)}{direct_path}"
    return {
        "item_id": media["id"],
        "title": media["title"],
        "resolution": attrs["resolution"] or "1080p",
        "hdr_format": hdr,
        "dv_profile_notice": attrs["has_dv"],
        "hls_configured": hls_configured,
        "hls_url": build_proxy_hls_url(token, jf_id if hls_configured else None),
        "direct_url": direct_path,
        # External players don't carry the session cookie that adult streams need.
        "vlc_url": None if media["is_adult"] else f"vlc://{full_direct}",
        "mpv_url": None if media["is_adult"] else f"mpv://{full_direct}",
        "infuse_url": None if media["is_adult"] else f"infuse://x-callback-url/play?url={quote(full_direct, safe='')}",
        "mpv_command": None if media["is_adult"] else f'mpv "{full_direct}"',
        "resume_seconds": resume_seconds,
        "duration_seconds": duration_seconds,
        "is_adult": media["is_adult"],
        "downloading": downloading,
        "streaming_mode": bool(qb_window and qb_window["streaming"]),
        "download_progress": round(qb_window["progress"] * 100.0, 1) if qb_window else None,
        "playable_progress": round(qb_window["playable"] * 100.0, 1) if downloading else None,
    }


@playback_router.get("/progress")
async def get_playback_progress(
    item_id: Optional[str] = Query(None),
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """Fetch cross-device SQLite playback resume state with Vault 404 stealth isolation."""
    user = await _require_active_db_user(session, db)
    vault_unlocked = check_vault_unlocked(session)

    if item_id is not None:
        clean_id = item_id.strip()
        if not vault_unlocked and await _hidden_while_locked(clean_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playback entry not found")
        row = (
            await db.execute(
                select(PlaybackProgress).where(
                    PlaybackProgress.user_id == user.id,
                    PlaybackProgress.item_id == clean_id,
                )
            )
        ).scalars().first()
        if not row:
            return PlaybackProgressResponse(item_id=clean_id)
        return _serialize_progress(row)

    rows: List[PlaybackProgress] = (
        await db.execute(
            select(PlaybackProgress)
            .where(PlaybackProgress.user_id == user.id)
            .order_by(PlaybackProgress.updated_at.desc())
            .limit(50)
        )
    ).scalars().all()
    safe_qb: Optional[set] = None
    if not vault_unlocked and any((r.item_id or "").lower().startswith("qb:") for r in rows):
        safe_qb = await _safe_qb_hashes()
    items = []
    for r in rows:
        if not vault_unlocked and await _hidden_while_locked(r.item_id, safe_qb if safe_qb is not None else set()):
            continue
        items.append(_serialize_progress(r))
    return {"items": items}


@playback_router.post("/progress", response_model=PlaybackProgressResponse)
async def save_playback_progress(
    body: PlaybackProgressUpdate,
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """Persist cross-device playback position into SQLite WAL for the current user."""
    user = await _require_active_db_user(session, db)
    clean_id = body.item_id.strip()
    if not check_vault_unlocked(session) and await _hidden_while_locked(clean_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Playback entry not found")

    pos = max(0.0, float(body.position_seconds))
    dur = max(0.0, float(body.duration_seconds))
    completed = bool(dur > 0 and (pos / dur) >= 0.92)

    row = (
        await db.execute(
            select(PlaybackProgress).where(
                PlaybackProgress.user_id == user.id,
                PlaybackProgress.item_id == clean_id,
            )
        )
    ).scalars().first()

    if row is None:
        row = PlaybackProgress(
            user_id=user.id,
            item_id=clean_id,
            title=(body.title or "").strip()[:512],
            position_seconds=pos,
            duration_seconds=dur,
            completed=completed,
            updated_at=utcnow(),
        )
        db.add(row)
    else:
        if body.title:
            row.title = body.title.strip()[:512]
        row.position_seconds = pos
        if dur > 0:
            row.duration_seconds = dur
        row.completed = completed
        row.updated_at = utcnow()

    await db.commit()
    await db.refresh(row)
    return _serialize_progress(row)


@router.get("/hls/{token}/{subpath:path}")
async def proxy_hls_stream(token: str, subpath: str, request: Request):
    payload = await _require_stream_token(token, request)
    raw_sub = request.url.path.split(f"/hls/{token}/", 1)[-1] if f"/hls/{token}/" in request.url.path else subpath
    clean_sub = subpath.lstrip("/")
    if (
        not clean_sub
        or "%" in raw_sub
        or "%" in clean_sub
        or "\\" in clean_sub
        or "//" in clean_sub
        or ".." in clean_sub.split("/")
        or not _HLS_SUBPATH_RE.fullmatch(clean_sub)
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid HLS asset path")
    jf_id = str(payload.get("jf_id") or "").strip()
    if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL or not jf_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HLS stream not configured for this item")

    filtered_pairs = [
        (k, v)
        for k, v in parse_qsl(request.url.query, keep_blank_values=False)
        if k.lower() in _ALLOWED_HLS_QUERY_KEYS
    ]
    if clean_sub == "master.m3u8" and not any(k.lower() == "mediasourceid" for k, _ in filtered_pairs):
        filtered_pairs.append(("MediaSourceId", jf_id))
    query = urlencode(filtered_pairs)
    upstream = f"{settings.JELLYFIN_URL.rstrip('/')}/Videos/{quote(jf_id)}/{clean_sub}" + (f"?{query}" if query else "")
    client = get_search_client()
    try:
        req = client.build_request("GET", upstream, headers=jellyfin_auth_headers())
        upstream_res = await client.send(req, stream=True)
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("Jellyfin HLS proxy failed (%s): %r", type(exc).__name__, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Jellyfin HLS upstream unreachable ({type(exc).__name__}).")

    if upstream_res.status_code >= 400:
        code = upstream_res.status_code
        await upstream_res.aclose()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY if code >= 500 else code, detail=f"Jellyfin HLS returned HTTP {code}.")

    ctype = upstream_res.headers.get("content-type") or (
        "application/vnd.apple.mpegurl" if clean_sub.endswith(".m3u8") else "video/mp2t"
    )
    if clean_sub.endswith(".m3u8") or "mpegurl" in ctype.lower():
        try:
            body_text = (await upstream_res.aread()).decode("utf-8", errors="replace")
        finally:
            await upstream_res.aclose()
        return Response(content=rewrite_m3u8_playlist(body_text, token, jf_id), media_type="application/vnd.apple.mpegurl")

    async def _stream_segment():
        try:
            async for chunk in upstream_res.aiter_bytes(CHUNK_SIZE):
                yield chunk
        finally:
            await upstream_res.aclose()

    return StreamingResponse(_stream_segment(), status_code=upstream_res.status_code, media_type=ctype)


@router.get("/file/{token}")
async def stream_media_file(token: str, request: Request):
    payload = await _require_stream_token(token, request)
    range_hdr = request.headers.get("range")
    raw_file_path = payload.get("file_path") or ""

    # Unfinished torrent: only ever send bytes whose pieces are downloaded. The
    # file is preallocated, so everything else reads back as zeros.
    if payload.get("qb_hash"):
        clamped = await _clamp_to_downloaded(payload["qb_hash"], range_hdr)
        if clamped is not None:
            raw_file_path, range_hdr = clamped

    local_file = resolve_local_media_path(raw_file_path)
    if local_file is not None:
        size = local_file.stat().st_size
        start, end = parse_range_header(range_hdr, size)
        length = (end - start) + 1
        headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
        if range_hdr:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(
            iter_file_range(local_file, start, length),
            status_code=206 if range_hdr else 200,
            media_type=guess_video_mime_type(str(local_file)),
            headers=headers,
        )

    if raw_file_path:
        try:
            sftp_res = await open_sftp_range_stream(raw_file_path, range_hdr)
            if sftp_res is not None:
                code, sftp_headers, mime, gen = sftp_res
                return StreamingResponse(gen, status_code=code, media_type=mime, headers=sftp_headers)
        except HTTPException:
            raise
        except FileNotFoundError:
            logger.warning("SFTP media file not found: %s", raw_file_path)
            if not (settings.JELLYFIN_API_KEY and settings.JELLYFIN_URL and payload.get("jf_id")):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file not found on seedbox")
        except Exception as exc:
            logger.warning("SFTP direct stream failed for %s: %s (%r)", raw_file_path, type(exc).__name__, exc)
            if not (settings.JELLYFIN_API_KEY and settings.JELLYFIN_URL and payload.get("jf_id")):
                raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Seedbox SFTP stream unreachable ({type(exc).__name__}).")

    if settings.JELLYFIN_API_KEY and settings.JELLYFIN_URL and payload.get("jf_id"):
        upstream = (
            f"{settings.JELLYFIN_URL.rstrip('/')}/Videos/{quote(str(payload['jf_id']))}/stream"
            "?static=true"
        )
        req_headers = jellyfin_auth_headers()
        if range_hdr:
            req_headers["Range"] = range_hdr
        client = get_search_client()
        try:
            upstream_res = await client.send(client.build_request("GET", upstream, headers=req_headers), stream=True)
        except (httpx.HTTPError, OSError) as exc:
            logger.warning("Jellyfin direct stream failed (%s): %r", type(exc).__name__, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Upstream Jellyfin stream unreachable ({type(exc).__name__}).",
            )
        fwd_headers = {
            k.lower(): v
            for k, v in upstream_res.headers.items()
            if k.lower() in ("content-range", "content-length", "accept-ranges", "content-type")
        }
        fwd_headers.setdefault("accept-ranges", "bytes")
        mime = fwd_headers.get("content-type") or guess_video_mime_type(payload.get("title") or "")

        async def _proxy_stream():
            try:
                async for chunk in upstream_res.aiter_bytes(CHUNK_SIZE):
                    yield chunk
            finally:
                await upstream_res.aclose()

        return StreamingResponse(_proxy_stream(), status_code=upstream_res.status_code, headers=fwd_headers, media_type=mime)

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media file path not available on stream proxy")


router.include_router(_user_router)
