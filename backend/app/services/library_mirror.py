"""Local SQLite FTS5 mirror of Radarr, Sonarr, and Whisparr media libraries."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import text

from backend.app.database import engine
from backend.app.services.arr_client import radarr_client, sonarr_client, whisparr_client
from backend.app.services.search_scoring import _normalize_title

logger = logging.getLogger("muuvies.library_mirror")

_SCHEMA_READY = False
_SYNC_LOCK = asyncio.Lock()

# Release noise words ignored when extracting FTS5 search terms from torrent titles
_NOISE_TOKENS = frozenset({
    "2160p", "1080p", "720p", "480p", "4k", "uhd", "fhd", "hd",
    "remux", "bluray", "bdrip", "brrip", "web", "webdl", "webrip", "hdtv",
    "x264", "x265", "h264", "h265", "hevc", "avc", "av1", "10bit",
    "hdr", "hdr10", "hdr10plus", "dovi", "dolby", "vision", "dv",
    "atmos", "truehd", "dts", "dtshd", "aac", "ac3", "eac3", "ddp5", "dd5", "flac",
    "proper", "repack", "extended", "unrated", "directors", "cut", "edition",
    "multi", "dual", "audio", "sub", "subs", "mkv", "mp4", "avi",
})


async def ensure_library_mirror_schema(force: bool = False) -> None:
    """Create the library_items table and its FTS5 virtual index if missing."""
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    async with engine.begin() as conn:
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS library_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service TEXT NOT NULL,
                    external_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    clean_title TEXT NOT NULL,
                    year INTEGER DEFAULT 0,
                    has_file INTEGER DEFAULT 0,
                    monitored INTEGER DEFAULT 1,
                    file_path TEXT DEFAULT '',
                    size_bytes BIGINT DEFAULT 0,
                    updated_at REAL NOT NULL,
                    UNIQUE(service, external_id)
                )
                """
            )
        )
        cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info(library_items)"))).fetchall()]
        if "file_path" not in cols:
            await conn.execute(text("ALTER TABLE library_items ADD COLUMN file_path TEXT DEFAULT ''"))
        if "size_bytes" not in cols:
            await conn.execute(text("ALTER TABLE library_items ADD COLUMN size_bytes BIGINT DEFAULT 0"))
        await conn.execute(
            text("CREATE INDEX IF NOT EXISTS ix_library_items_service ON library_items (service)")
        )
        await conn.execute(
            text(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS library_items_fts USING fts5(
                    title,
                    clean_title,
                    service UNINDEXED,
                    year UNINDEXED,
                    has_file UNINDEXED,
                    external_id UNINDEXED,
                    content='library_items',
                    content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                )
                """
            )
        )
    _SCHEMA_READY = True


def reset_schema_flag() -> None:
    """Reset cached schema readiness (useful when tests recreate SQLite schemas)."""
    global _SCHEMA_READY
    _SCHEMA_READY = False


async def replace_service_library_items(service: str, items: List[Dict[str, Any]]) -> int:
    """Replace all mirrored library rows for one *arr service and rebuild the FTS5 index."""
    await ensure_library_mirror_schema()
    svc = service.strip().lower()
    now = time.time()

    rows = []
    for idx, it in enumerate(items):
        title = str(it.get("title") or "").strip()
        if not title:
            continue
        ext_id = it.get("id")
        if ext_id is None:
            ext_id = idx + 1
        clean_title = str(it.get("clean_title") or re.sub(r"[^a-zA-Z0-9]", "", title.lower()))
        size_val = it.get("size_bytes") or it.get("sizeOnDisk") or (it.get("statistics") or {}).get("sizeOnDisk") or 0
        rows.append({
            "service": svc,
            "external_id": int(ext_id),
            "title": title,
            "clean_title": clean_title,
            "year": int(it.get("year") or 0),
            "has_file": 1 if it.get("has_file") else 0,
            "monitored": 1 if it.get("monitored", True) else 0,
            "file_path": str(it.get("file_path") or ""),
            "size_bytes": int(size_val or 0),
            "updated_at": now,
        })

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM library_items WHERE service = :svc"), {"svc": svc})
        if rows:
            await conn.execute(
                text(
                    """
                    INSERT OR REPLACE INTO library_items
                        (service, external_id, title, clean_title, year, has_file, monitored, file_path, size_bytes, updated_at)
                    VALUES
                        (:service, :external_id, :title, :clean_title, :year, :has_file, :monitored, :file_path, :size_bytes, :updated_at)
                    """
                ),
                rows,
            )
        await conn.execute(text("INSERT INTO library_items_fts(library_items_fts) VALUES('rebuild')"))
    return len(rows)


async def update_library_item_file_path(service: str, external_id: int, file_path: str) -> None:
    """Persist an on-demand resolved media file_path back into library_items."""
    await ensure_library_mirror_schema()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE library_items SET file_path = :fp, has_file = 1, updated_at = :ts "
                "WHERE service = :svc AND external_id = :ext"
            ),
            {"fp": file_path, "ts": time.time(), "svc": service.strip().lower(), "ext": int(external_id)},
        )


async def sync_library_mirror() -> Dict[str, int]:
    """Fetch Radarr, Sonarr, and Whisparr libraries concurrently and mirror them into SQLite."""
    await ensure_library_mirror_schema()
    async with _SYNC_LOCK:
        results = await asyncio.gather(
            radarr_client.fetch_library_titles_with_status(),
            sonarr_client.fetch_library_titles_with_status(),
            whisparr_client.fetch_library_titles_with_status(),
            return_exceptions=True,
        )
        counts: Dict[str, int] = {}
        for svc_name, res in zip(("radarr", "sonarr", "whisparr"), results):
            if isinstance(res, tuple):
                ok, items = res
                if ok:
                    counts[svc_name] = await replace_service_library_items(svc_name, items)
        return counts


def _extract_fts_tokens(texts: Sequence[str], max_tokens: int = 16) -> List[str]:
    """Extract clean alphanumeric search tokens suitable for safe FTS5 MATCH expressions."""
    tokens: List[str] = []
    seen = set()
    for raw in texts:
        if not raw:
            continue
        for tok in re.findall(r"[a-zA-Z0-9]{2,}", str(raw).lower()):
            if tok in _NOISE_TOKENS or tok in seen:
                continue
            if re.fullmatch(r"(19|20)\d{2}", tok):
                # Years help disambiguate less than title words if lots of tokens exist; keep only if short
                if len(tokens) >= 6:
                    continue
            seen.add(tok)
            tokens.append(tok)
            if len(tokens) >= max_tokens:
                return tokens
    return tokens


async def query_library_fts(
    query_terms: Sequence[str] | str,
    vault_unlocked: bool = False,
    limit: int = 40,
) -> List[Dict[str, Any]]:
    """Query the local SQLite FTS5 library mirror in <2ms without network round-trips."""
    await ensure_library_mirror_schema()

    terms_seq = [query_terms] if isinstance(query_terms, str) else list(query_terms)
    tokens = _extract_fts_tokens(terms_seq)
    if not tokens:
        return []

    fts_expr = " OR ".join(f'"{tok}"*' for tok in tokens)
    allowed_services = ("radarr", "sonarr", "whisparr") if vault_unlocked else ("radarr", "sonarr")
    svc_placeholders = ", ".join(f"'{s}'" for s in allowed_services)

    sql = text(
        f"""
        SELECT l.service, l.external_id, l.title, l.clean_title, l.year, l.has_file, l.monitored
        FROM library_items_fts f
        JOIN library_items l ON l.id = f.rowid
        WHERE library_items_fts MATCH :fts_expr
          AND l.service IN ({svc_placeholders})
        ORDER BY bm25(library_items_fts)
        LIMIT :limit
        """
    )

    try:
        async with engine.connect() as conn:
            res = await conn.execute(sql, {"fts_expr": fts_expr, "limit": int(limit)})
            rows = res.fetchall()
    except Exception as exc:
        logger.debug("FTS5 mirror query skipped or failed: %s", exc)
        return []

    return [
        {
            "service": row[0],
            "id": row[1],
            "title": row[2],
            "clean_title": row[3],
            "year": row[4],
            "has_file": bool(row[5]),
            "monitored": bool(row[6]),
        }
        for row in rows
    ]


async def get_library_overview_from_mirror(vault_unlocked: bool = False) -> Dict[str, Any]:
    """Return Radarr/Sonarr/(Whisparr) items from the local SQLite mirror (<2ms)."""
    await ensure_library_mirror_schema()
    allowed_services = ("radarr", "sonarr", "whisparr") if vault_unlocked else ("radarr", "sonarr")
    svc_placeholders = ", ".join(f"'{s}'" for s in allowed_services)

    async with engine.connect() as conn:
        res = await conn.execute(
            text(
                f"""
                SELECT service, external_id, title, clean_title, year, has_file, monitored
                FROM library_items
                WHERE service IN ({svc_placeholders})
                ORDER BY title ASC
                """
            )
        )
        rows = res.fetchall()

    # Cold-start convenience: if mirror is empty on first request, try one live sync
    if not rows:
        counts = await sync_library_mirror()
        if any(counts.values()):
            async with engine.connect() as conn:
                res = await conn.execute(
                    text(
                        f"""
                        SELECT service, external_id, title, clean_title, year, has_file, monitored
                        FROM library_items
                        WHERE service IN ({svc_placeholders})
                        ORDER BY title ASC
                        """
                    )
                )
                rows = res.fetchall()

    movies: List[Dict[str, Any]] = []
    shows: List[Dict[str, Any]] = []
    adult: List[Dict[str, Any]] = []

    for svc, ext_id, title, clean_title, year, has_file, monitored in rows:
        item = {
            "id": ext_id,
            "title": title,
            "clean_title": clean_title,
            "year": year,
            "has_file": bool(has_file),
            "monitored": bool(monitored),
        }
        if svc == "radarr":
            movies.append(item)
        elif svc == "sonarr":
            shows.append(item)
        elif svc == "whisparr" and vault_unlocked:
            adult.append(item)

    return {
        "movies": movies,
        "shows": shows,
        "adult": adult,
        "total_movies": len(movies),
        "total_shows": len(shows),
        "total_adult": len(adult),
    }


_SUGGEST_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_SUGGEST_RES_RE = re.compile(r"\b(2160p|4k|uhd|1080p|720p)\b", re.IGNORECASE)


def _extract_year_and_res(title: str, explicit_year: Any = None) -> tuple[Optional[int], Optional[str]]:
    year: Optional[int] = None
    if isinstance(explicit_year, int) and explicit_year > 1900:
        year = explicit_year
    else:
        ym = _SUGGEST_YEAR_RE.search(title or "")
        if ym:
            year = int(ym.group(1))
    rm = _SUGGEST_RES_RE.search(title or "")
    res: Optional[str] = None
    if rm:
        raw_res = rm.group(1).upper()
        res = "2160p" if raw_res in {"2160P", "4K", "UHD"} else raw_res.lower()
    return year, res


async def suggest_local_prefix(
    query: str,
    vault_unlocked: bool = False,
    limit: int = 8,
) -> List[Dict[str, Any]]:
    """Fast (<15ms) local SQLite FTS5 + grab_history prefix autocomplete with Vault isolation."""
    norm_q = _normalize_title(query or "")
    if len(norm_q) < 2:
        return []

    await ensure_library_mirror_schema()
    tokens = [tok for tok in re.findall(r"[a-z0-9]+", norm_q) if tok][:8]
    like_pat = f"%{norm_q}%"
    allowed_services = ("radarr", "sonarr", "whisparr") if vault_unlocked else ("radarr", "sonarr")
    svc_placeholders = ", ".join(f"'{s}'" for s in allowed_services)

    suggestions: List[Dict[str, Any]] = []
    seen_norm: set[str] = set()

    def _push(title: str, source: str, explicit_year: Any = None) -> None:
        if len(suggestions) >= limit:
            return
        clean = str(title or "").strip()
        if not clean:
            return
        key = _normalize_title(clean)
        if not key or key in seen_norm:
            return
        seen_norm.add(key)
        year, res = _extract_year_and_res(clean, explicit_year=explicit_year)
        suggestions.append({"title": clean, "source": source, "year": year, "res": res})

    try:
        async with engine.connect() as conn:
            lib_rows = []
            if tokens:
                fts_expr = " ".join(f'"{tok}"*' for tok in tokens)
                try:
                    res = await conn.execute(
                        text(
                            f"""
                            SELECT l.title, l.year
                            FROM library_items_fts f
                            JOIN library_items l ON l.id = f.rowid
                            WHERE library_items_fts MATCH :fts_expr
                              AND lower(l.service) IN ({svc_placeholders})
                            ORDER BY bm25(library_items_fts)
                            LIMIT :limit
                            """
                        ),
                        {"fts_expr": fts_expr, "limit": int(limit)},
                    )
                    lib_rows = res.fetchall()
                except Exception:
                    lib_rows = []

            if not lib_rows:
                res = await conn.execute(
                    text(
                        f"""
                        SELECT title, year
                        FROM library_items
                        WHERE lower(service) IN ({svc_placeholders})
                          AND (lower(title) LIKE :like_pat OR lower(clean_title) LIKE :like_pat)
                        ORDER BY title ASC
                        LIMIT :limit
                        """
                    ),
                    {"like_pat": like_pat, "limit": int(limit)},
                )
                lib_rows = res.fetchall()

            for row in lib_rows:
                _push(row[0], "library", explicit_year=row[1])

            if len(suggestions) < limit:
                vault_clause = ""
                if not vault_unlocked:
                    vault_clause = (
                        "AND lower(category) NOT IN ('adult', 'xxx') "
                        "AND lower(category) NOT LIKE '%adult%' "
                        "AND lower(category) NOT LIKE '%xxx%' "
                        "AND lower(target) != 'whisparr'"
                    )
                hist_sql = text(
                    f"""
                    SELECT title
                    FROM grab_history
                    WHERE lower(title) LIKE :like_pat
                      {vault_clause}
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                )
                hist_rows = (await conn.execute(hist_sql, {"like_pat": like_pat, "limit": int(limit * 2)})).fetchall()
                for row in hist_rows:
                    _push(row[0], "history")
                    if len(suggestions) >= limit:
                        break
    except Exception as exc:
        logger.debug("Local prefix suggest skipped: %s", exc)

    return suggestions


async def get_library_audit_rows(vault_unlocked: bool = False) -> List[Dict[str, Any]]:
    """Return mirrored library rows with file_path and size_bytes for duplicate/heavy-file audit."""
    await ensure_library_mirror_schema()
    allowed_services = ("radarr", "sonarr", "whisparr") if vault_unlocked else ("radarr", "sonarr")
    svc_placeholders = ", ".join(f"'{s}'" for s in allowed_services)
    try:
        async with engine.connect() as conn:
            res = await conn.execute(
                text(
                    f"""
                    SELECT service, external_id, title, clean_title, year, has_file, file_path, size_bytes
                    FROM library_items
                    WHERE lower(service) IN ({svc_placeholders})
                    ORDER BY service ASC, title ASC
                    """
                )
            )
            return [
                {
                    "service": row[0],
                    "external_id": int(row[1]),
                    "title": row[2],
                    "clean_title": row[3],
                    "year": int(row[4] or 0),
                    "has_file": bool(row[5]),
                    "file_path": row[6] or "",
                    "size_bytes": int(row[7] or 0),
                }
                for row in res.fetchall()
            ]
    except Exception as exc:
        logger.debug("Library audit query failed: %s", exc)
        return []


async def run_library_sync_loop(interval_seconds: int = 600) -> None:
    """Periodic background loop mirroring *arr libraries into SQLite every 10 minutes."""
    while True:
        try:
            counts = await sync_library_mirror()
            if counts:
                logger.info("Synced local SQLite FTS5 library mirror: %s", counts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Library mirror sync iteration failed: %s", exc)
        await asyncio.sleep(interval_seconds)

