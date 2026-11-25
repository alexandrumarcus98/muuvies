"""Fernet-encrypted stream tokens, HTTP 206 byte-range parsing, and media source resolution."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import posixpath
import re
import stat
import time
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional, Tuple
from urllib.parse import quote

from fastapi import HTTPException, status
from sqlalchemy import text

try:
    import asyncssh
except ImportError:  # pragma: no cover - optional in bare environments
    asyncssh = None  # type: ignore[assignment]

from backend.app.config import settings
from backend.app.database import engine
from backend.app.security import decrypt_text, encrypt_text
from backend.app.services.arr_client import radarr_client, sonarr_client, whisparr_client
from backend.app.services.http_clients import get_telemetry_client
from backend.app.services.library_mirror import ensure_library_mirror_schema, update_library_item_file_path
from backend.app.services.qbittorrent import qb_client
from backend.app.services.url_safety import has_control_chars

logger = logging.getLogger("muuvies.stream")

CHUNK_SIZE = 512 * 1024
TOKEN_TTL_SECONDS = 4 * 3600
PREVIEW_TOKEN_TTL_SECONDS = 3600
PREVIEW_IMAGE_TOKEN_TTL_SECONDS = 900
VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".m4v", ".webm", ".avi", ".mov", ".ts"})
_EXPLICIT_VIDEO_MIMES = {
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".avi": "video/x-msvideo",
    ".mov": "video/quicktime",
    ".ts": "video/mp2t",
}


def guess_video_mime_type(path_or_name: str, fallback: str = "video/mp4") -> str:
    """Return explicit MIME type for common cinema containers (.mp4, .mkv, etc.)."""
    suffix = posixpath.splitext((path_or_name or "").strip().lower())[1]
    if suffix in _EXPLICIT_VIDEO_MIMES:
        return _EXPLICIT_VIDEO_MIMES[suffix]
    guessed = mimetypes.guess_type(path_or_name)[0]
    return guessed or fallback


def jellyfin_auth_headers() -> Dict[str, str]:
    """Return Jellyfin MediaBrowser token header (required by reverse proxy & Jellyfin 10.9+)."""
    key = (settings.JELLYFIN_API_KEY or "").strip()
    return {"Authorization": f'MediaBrowser Token="{key}"'} if key else {}


def create_stream_token(payload: Dict[str, Any], ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
    """Create a 4-hour URL-safe Fernet-encrypted stream token (keeps file_path opaque)."""
    body = {**payload, "kind": "stream", "exp": int(time.time()) + ttl_seconds}
    fernet_tok = encrypt_text(json.dumps(body, separators=(",", ":")))
    return f"f1.{fernet_tok}"


def verify_stream_token(token: str) -> Optional[Dict[str, Any]]:
    """Decrypt and verify expiry of a Fernet stream token."""
    try:
        prefix, fernet_tok = token.split(".", 1)
        if prefix != "f1" or not fernet_tok:
            return None
        data = json.loads(decrypt_text(fernet_tok))
        if not isinstance(data, dict):
            return None
        token_kind = data.get("kind")
        if token_kind is not None and token_kind != "stream":
            return None
        return data if int(data.get("exp", 0)) >= int(time.time()) else None
    except Exception:
        return None


def create_preview_token(
    payload: Dict[str, Any],
    *,
    vault_exp: Optional[int] = None,
    ttl_seconds: int = PREVIEW_TOKEN_TTL_SECONDS,
    kind: str = "adult-preview",
) -> str:
    """Create a Fernet-encrypted adult preview token bounded to the Vault unlock window."""
    exp = int(time.time()) + max(1, int(ttl_seconds))
    if isinstance(vault_exp, (int, float)) and int(vault_exp) > 0:
        exp = min(exp, int(vault_exp))
    body = {**payload, "kind": kind, "exp": exp}
    fernet_tok = encrypt_text(json.dumps(body, separators=(",", ":")))
    return f"f1.{fernet_tok}"


def verify_preview_token(
    token: str,
    *,
    expected_kind: str = "adult-preview",
) -> Optional[Dict[str, Any]]:
    """Decrypt and verify kind + expiry of a Fernet adult preview token."""
    try:
        prefix, fernet_tok = token.split(".", 1)
        if prefix != "f1" or not fernet_tok:
            return None
        data = json.loads(decrypt_text(fernet_tok))
        if not isinstance(data, dict):
            return None
        if data.get("kind") != expected_kind:
            return None
        return data if int(data.get("exp", 0)) >= int(time.time()) else None
    except Exception:
        return None


def parse_range_header(range_header: Optional[str], total_size: int) -> Tuple[int, int]:
    """Parse an HTTP Range header into inclusive (start, end) byte bounds."""
    if total_size <= 0:
        return 0, 0
    if not range_header:
        return 0, total_size - 1
    match = re.match(r"bytes=(\d*)-(\d*)$", range_header.strip())
    if not match or (match.group(1) == "" and match.group(2) == ""):
        raise HTTPException(status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE, detail="Invalid Range header")
    if match.group(1) == "":
        start = max(0, total_size - int(match.group(2)))
        end = total_size - 1
    else:
        start = int(match.group(1))
        end = min(int(match.group(2)), total_size - 1) if match.group(2) else total_size - 1
    if start >= total_size or start > end:
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="Requested range not satisfiable",
            headers={"Content-Range": f"bytes */{total_size}"},
        )
    return start, end


def build_jellyfin_hls_url(media_id: str, base_url: Optional[str] = None, api_key: Optional[str] = None) -> str:
    """Build Jellyfin 4K video-copy / AAC audio-remux HLS master playlist URL."""
    root = (base_url or settings.JELLYFIN_URL).rstrip("/")
    key_q = f"&api_key={quote(api_key)}" if api_key else ""
    return (
        f"{root}/Videos/{quote(str(media_id))}/master.m3u8"
        f"?VideoCodec=hevc,h264&AllowVideoStreamCopy=true&AudioCodec=aac"
        f"&TranscodingMaxAudioChannels=2&MaxStreamingBitrate=120000000{key_q}"
    )


def build_proxy_hls_url(token: str, media_id: Optional[str] = None) -> str:
    """Build internal Muuvies HLS proxy URL with 4K direct video copy & AAC audio remux."""
    source_q = f"&MediaSourceId={quote(str(media_id))}" if media_id else ""
    return (
        f"/api/stream/hls/{token}/master.m3u8"
        f"?VideoCodec=hevc,h264&AllowVideoStreamCopy=true&AudioCodec=aac"
        f"&TranscodingMaxAudioChannels=2&MaxStreamingBitrate=120000000{source_q}"
    )


def rewrite_m3u8_playlist(content: str, token: str, jf_id: str) -> str:
    """Rewrite any absolute Jellyfin /Videos/{jf_id}/ URIs in an m3u8 playlist through our proxy."""
    proxy_prefix = f"/api/stream/hls/{token}/"
    pattern = re.compile(rf"(?:https?://[^/\s\"']+)?(?:[^\"\s]*)/Videos/{re.escape(str(jf_id))}/")
    return pattern.sub(proxy_prefix, content)


def has_media_extension(path_or_name: str) -> bool:
    """Return True if path_or_name ends with a supported video file extension."""
    return posixpath.splitext((path_or_name or "").strip().lower())[1] in VIDEO_EXTENSIONS


async def resolve_jellyfin_id(title: str, fallback_id: str, file_path: str = "") -> Tuple[str, bool]:
    if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
        return fallback_id, False
    cleaned = re.split(
        r"\b(2160p|1080p|720p|4k|s\d{2}e\d{2}|bluray|remux|web-dl)\b|\(\d{4}\)",
        title.replace(".", " "),
        flags=re.I,
    )[0].strip(" -_.")
    search_term = cleaned or title
    try:
        res = await get_telemetry_client().get(
            f"{settings.JELLYFIN_URL.rstrip('/')}/Items",
            headers=jellyfin_auth_headers(),
            params={
                "SearchTerm": search_term,
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Episode",
                "Fields": "Path,ProductionYear",
                "Limit": 10,
            },
            timeout=2.0,
        )
        if res.status_code == 200:
            items = (res.json() or {}).get("Items") or []
            fp_norm = posixpath.normpath(file_path) if file_path else ""
            fp_base = posixpath.basename(fp_norm) if fp_norm else ""
            if fp_norm:
                for it in items:
                    it_path = posixpath.normpath(str(it.get("Path") or ""))
                    if it.get("Id") and (it_path == fp_norm or (fp_base and posixpath.basename(it_path) == fp_base)):
                        return str(it["Id"]), True
            # No exact file match: only accept a search hit that is the same
            # episode (SxxEyy) or, for films, the same year. Taking the first
            # hit blindly played the wrong episode of a show.
            ep = re.search(r"\bS(\d{1,2})E(\d{1,3})\b", title, flags=re.I)
            year = re.search(r"\b(19\d{2}|20\d{2})\b", title)
            for it in items:
                if not it.get("Id"):
                    continue
                if ep:
                    if it.get("Type") == "Episode" and it.get("ParentIndexNumber") == int(ep.group(1)) \
                            and it.get("IndexNumber") == int(ep.group(2)):
                        return str(it["Id"]), True
                    continue
                if it.get("Type") != "Movie":
                    continue
                if year and it.get("ProductionYear") and int(it["ProductionYear"]) != int(year.group(1)):
                    continue
                return str(it["Id"]), True
    except Exception as exc:
        logger.warning("Jellyfin item lookup failed for %r: %s (%r)", search_term, type(exc).__name__, exc)
    return fallback_id, False


async def resolve_media_item(item_id: str, vault_unlocked: bool, title_hint: Optional[str] = None) -> Dict[str, Any]:
    if item_id.startswith("qb:"):
        target_hash = item_id[3:].lower()
        for t in await qb_client.get_queue(filter_status="all"):
            if str(t.get("hash", "")).lower() == target_hash:
                cat = (t.get("category") or "").lower()
                is_adult = cat == "adult" or "xxx" in cat
                if is_adult and not vault_unlocked:
                    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media item not found")
                return {
                    "id": item_id,
                    "title": t.get("name") or title_hint or target_hash,
                    "is_adult": is_adult,
                    "file_path": t.get("content_path") or "",
                }
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media item not found")

    await ensure_library_mirror_schema()
    svc, ext_id = item_id.split(":", 1) if ":" in item_id else (None, item_id)
    async with engine.connect() as conn:
        if svc in ("radarr", "sonarr", "whisparr") and ext_id.isdigit():
            row = (await conn.execute(
                text(
                    "SELECT service, external_id, title, COALESCE(file_path, '') "
                    "FROM library_items WHERE service=:s AND external_id=:e"
                ),
                {"s": svc, "e": int(ext_id)},
            )).fetchone()
        elif item_id.isdigit():
            row = (await conn.execute(
                text(
                    "SELECT service, external_id, title, COALESCE(file_path, '') "
                    "FROM library_items WHERE id=:i OR external_id=:i ORDER BY id ASC LIMIT 1"
                ),
                {"i": int(item_id)},
            )).fetchone()
        else:
            q_title = (title_hint or (item_id[7:] if item_id.startswith("search:") else item_id)).strip().lower()
            clean_q = re.sub(r"[^a-z0-9]", "", q_title)
            row = (await conn.execute(
                text(
                    "SELECT service, external_id, title, COALESCE(file_path, '') "
                    "FROM library_items WHERE clean_title=:c OR lower(title)=:t LIMIT 1"
                ),
                {"c": clean_q, "t": q_title},
            )).fetchone()

    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media item not found")
    is_adult = row[0] == "whisparr"
    if is_adult and not vault_unlocked:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Media item not found")
    return {"id": f"{row[0]}:{row[1]}", "title": row[2], "is_adult": is_adult, "file_path": row[3] or ""}


async def resolve_arr_item_file_path(item_id: str, current_path: str) -> str:
    """Resolve and persist missing or directory-only file_path from Radarr/Sonarr/Whisparr."""
    if (current_path or "").strip() and has_media_extension(current_path):
        return current_path
    if ":" not in item_id:
        return current_path
    svc, ext_id = item_id.split(":", 1)
    if not ext_id.isdigit():
        return current_path
    client_map = {"radarr": radarr_client, "sonarr": sonarr_client, "whisparr": whisparr_client}
    client = client_map.get(svc)
    if client is None:
        return current_path
    resolved = await client.fetch_item_file_path(int(ext_id))
    if resolved:
        await update_library_item_file_path(svc, int(ext_id), resolved)
        return resolved
    return current_path


def resolve_local_media_path(raw_path: str) -> Optional[Path]:
    raw = (raw_path or "").strip()
    media_root = (settings.STREAM_MEDIA_ROOT or "").strip()
    if not raw or not media_root or has_control_chars(raw):
        return None
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(media_root) / candidate
    try:
        root_resolved = Path(media_root).resolve()
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root_resolved):
            return None
        if not resolved.is_file() or not has_media_extension(resolved.name):
            return None
        return resolved
    except Exception:
        return None


async def iter_file_range(path: Path, start: int, length: int) -> AsyncIterator[bytes]:
    fh = await asyncio.to_thread(open, path, "rb")
    try:
        if start > 0:
            await asyncio.to_thread(fh.seek, start)
        remaining = length
        while remaining > 0:
            chunk = await asyncio.to_thread(fh.read, min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        await asyncio.to_thread(fh.close)


def _allowed_seedbox_prefixes() -> Tuple[str, ...]:
    prefixes = []
    for raw_dir in (
        settings.STREAM_MEDIA_ROOT,
        settings.RADARR_ROOT_FOLDER,
        settings.SONARR_ROOT_FOLDER,
        settings.WHISPARR_ROOT_FOLDER,
    ):
        cleaned = (raw_dir or "").strip()
        if cleaned and cleaned.startswith("/"):
            norm_dir = posixpath.normpath(cleaned)
            if norm_dir != "/":
                prefixes.append(norm_dir)

    seedbox_user = (settings.SEEDBOX_USER or "").strip()
    if seedbox_user and not has_control_chars(seedbox_user) and "/" not in seedbox_user and seedbox_user not in (".", ".."):
        for sub in ("media", "torrents", "downloads", "files"):
            prefixes.append(posixpath.normpath(f"/home/{seedbox_user}/{sub}"))

    return tuple(dict.fromkeys(prefixes))


def _resolve_seedbox_sftp_path(raw_path: str) -> Optional[str]:
    """Validate that a remote POSIX path sits inside allowed seedbox media/download directories."""
    raw = (raw_path or "").strip()
    if not raw or has_control_chars(raw):
        return None

    allowed_dirs = _allowed_seedbox_prefixes()
    if not allowed_dirs:
        return None

    if not raw.startswith("/"):
        media_root = (settings.STREAM_MEDIA_ROOT or "").strip()
        root = posixpath.normpath(media_root) if media_root else allowed_dirs[0]
        candidate = posixpath.join(root, raw)
    else:
        candidate = raw

    if any(seg == ".." or seg.startswith(".") for seg in candidate.split("/") if seg):
        return None

    norm = posixpath.normpath(candidate)
    if any(seg == ".." or seg.startswith(".") for seg in norm.split("/") if seg):
        return None

    for prefix in allowed_dirs:
        if norm == prefix or norm.startswith(f"{prefix}/"):
            return norm
    return None


def _get_ssh_credentials() -> Tuple[Optional[str], Optional[str]]:
    """Locate the read-only seedbox SSH private key and required known_hosts file (fail closed)."""
    for ssh_dir in (Path("/home/appuser/.ssh"), Path.home() / ".ssh"):
        kh_path = ssh_dir / "known_hosts"
        if not kh_path.is_file():
            continue
        try:
            if kh_path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        for key_name in ("id_ed25519", "id_rsa", "id_ecdsa"):
            key_path = ssh_dir / key_name
            if key_path.is_file():
                return str(key_path), str(kh_path)
    return None, None


async def _pick_sftp_video_file(sftp: Any, target_path: str, depth: int = 0) -> Tuple[str, int]:
    """Resolve a remote video file or pick the largest non-sample video file inside a folder."""
    attrs = await sftp.stat(target_path)
    if attrs.permissions is not None and stat.S_ISDIR(attrs.permissions):
        best: Optional[Tuple[int, int, str]] = None
        for entry in await sftp.readdir(target_path):
            name = str(entry.filename or "")
            if name in (".", "..") or name.startswith("."):
                continue
            child_path = posixpath.join(target_path, name)
            child_attrs = entry.attrs
            if child_attrs.permissions is not None and stat.S_ISDIR(child_attrs.permissions) and depth < 1:
                try:
                    sub_path, sub_size = await _pick_sftp_video_file(sftp, child_path, depth + 1)
                    is_sample = 0 if "sample" in name.lower() else 1
                    score = (is_sample, sub_size, sub_path)
                    if best is None or score > best:
                        best = score
                except FileNotFoundError:
                    continue
                continue
            ext = posixpath.splitext(name.lower())[1]
            if ext in VIDEO_EXTENSIONS:
                size = int(child_attrs.size or 0)
                is_sample = 0 if "sample" in name.lower() else 1
                score = (is_sample, size, child_path)
                if best is None or score > best:
                    best = score
        if best is None:
            raise FileNotFoundError(f"No streamable video found inside {target_path}")
        return best[2], best[1]
    if attrs.permissions is not None and not stat.S_ISREG(attrs.permissions):
        raise FileNotFoundError(f"Not a regular video file: {target_path}")
    if not has_media_extension(target_path):
        raise FileNotFoundError(f"Unsupported non-video file: {target_path}")
    return target_path, int(attrs.size or 0)


class _SFTPPool:
    """Bounded multi-session async SSH/SFTP pool for concurrent byte-range scrubbing."""

    def __init__(self, max_sessions: int = 3) -> None:
        self._lock = asyncio.Lock()
        self._max_sessions = max(1, int(max_sessions))
        self._slots: list[tuple[Any, Any]] = []
        self._next_idx = 0

    async def acquire(self) -> Any:
        key_path, known_hosts = _get_ssh_credentials()
        if (
            asyncssh is None
            or not key_path
            or not known_hosts
            or not (settings.SEEDBOX_HOST or "").strip()
            or not (settings.SEEDBOX_USER or "").strip()
        ):
            return None
        async with self._lock:
            self._slots = [
                (conn, sftp)
                for conn, sftp in self._slots
                if conn is not None and not conn.is_closed() and sftp is not None
            ]
            if len(self._slots) < self._max_sessions:
                conn = await asyncssh.connect(
                    host=settings.SEEDBOX_HOST,
                    port=settings.SEEDBOX_SSH_PORT,
                    username=settings.SEEDBOX_USER,
                    client_keys=[key_path],
                    known_hosts=known_hosts,
                    connect_timeout=5.0,
                    keepalive_interval=30,
                )
                sftp = await conn.start_sftp_client()
                self._slots.append((conn, sftp))
                return sftp

            idx = self._next_idx % len(self._slots)
            self._next_idx += 1
            return self._slots[idx][1]

    async def invalidate(self, bad_sftp: Any = None) -> None:
        async with self._lock:
            if bad_sftp is None:
                for conn, sftp in self._slots:
                    self._close_pair(conn, sftp)
                self._slots.clear()
                return

            kept = []
            for conn, sftp in self._slots:
                if sftp is bad_sftp:
                    self._close_pair(conn, sftp)
                else:
                    kept.append((conn, sftp))
            self._slots = kept

    @staticmethod
    def _close_pair(conn: Any, sftp: Any) -> None:
        if sftp is not None:
            try:
                sftp.exit()
            except Exception:
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_sftp_pool = _SFTPPool(max_sessions=3)


async def close_sftp_pool() -> None:
    """Close cached SSH/SFTP streaming session during application shutdown."""
    await _sftp_pool.invalidate()


async def open_sftp_range_stream(
    raw_path: str,
    range_header: Optional[str],
) -> Optional[Tuple[int, Dict[str, str], str, AsyncIterator[bytes]]]:
    """Open a pooled SSH/SFTP byte-range stream for a remote seedbox media file."""
    remote_path = _resolve_seedbox_sftp_path(raw_path)
    if not remote_path:
        return None

    sftp = None
    file_path = ""
    total_size = 0
    for attempt in range(2):
        sftp = await _sftp_pool.acquire()
        if sftp is None:
            return None
        try:
            file_path, total_size = await _pick_sftp_video_file(sftp, remote_path)
            break
        except FileNotFoundError:
            raise
        except Exception:
            await _sftp_pool.invalidate(sftp)
            if attempt == 1:
                raise

    if not has_media_extension(file_path):
        raise FileNotFoundError(f"Unsupported non-video file: {file_path}")

    start, end = parse_range_header(range_header, total_size)
    length = (end - start) + 1
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length)}
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{total_size}"
    mime = guess_video_mime_type(file_path)

    async def _iter_sftp() -> AsyncIterator[bytes]:
        remote_fh = None
        try:
            remote_fh = await sftp.open(file_path, "rb")
            if start > 0:
                await remote_fh.seek(start)
            remaining = length
            while remaining > 0:
                chunk = await remote_fh.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception:
            await _sftp_pool.invalidate(sftp)
            raise
        finally:
            if remote_fh is not None:
                try:
                    await remote_fh.close()
                except Exception:
                    pass

    return (206 if range_header else 200), headers, mime, _iter_sftp()
