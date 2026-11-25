"""Helpers for watching a torrent while qBittorrent is still downloading it.

- Info-hash extraction from .torrent bytes and magnet URIs, so a grab can hand
  the player a `qb:<hash>` id straight away.
- Piece maths: which bytes of a file are already on disk, so the stream proxy
  never sends the zero-filled gaps of a preallocated, half-downloaded file.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

# qBittorrent /api/v2/torrents/pieceStates: 0 = missing, 1 = downloading, 2 = have.
PIECE_HAVE = 2

_HEX40 = re.compile(r"\A[0-9a-fA-F]{40}\Z")
_B32 = re.compile(r"\A[A-Z2-7]{32}\Z")

VIDEO_EXTENSIONS = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".wmv", ".mpg", ".mpeg")


# ---------------------------------------------------------------------------
# Info-hash
# ---------------------------------------------------------------------------

def _bencode_end(data: bytes, i: int) -> int:
    """Return the index just past the bencoded value that starts at ``i``."""
    c = data[i:i + 1]
    if c == b"i":
        j = data.index(b"e", i)
        return j + 1
    if c in (b"l", b"d"):
        i += 1
        while data[i:i + 1] != b"e":
            i = _bencode_end(data, i)
        return i + 1
    if c.isdigit():
        colon = data.index(b":", i)
        length = int(data[i:colon])
        return colon + 1 + length
    raise ValueError("invalid bencode")


def torrent_info_hash(data: bytes) -> Optional[str]:
    """SHA-1 v1 info-hash (lowercase hex) of a .torrent file, or None if malformed."""
    try:
        if not data or data[:1] != b"d":
            return None
        i = 1
        while data[i:i + 1] != b"e":
            key_end = _bencode_end(data, i)
            colon = data.index(b":", i)
            key = data[colon + 1:key_end]
            value_end = _bencode_end(data, key_end)
            if key == b"info":
                return hashlib.sha1(data[key_end:value_end]).hexdigest()
            i = value_end
    except (ValueError, IndexError):
        return None
    return None


def magnet_info_hash(uri: str) -> Optional[str]:
    """Lowercase hex v1 info-hash from a magnet URI (hex or base32 btih), or None."""
    try:
        parsed = urlparse(uri or "")
    except ValueError:
        return None
    if parsed.scheme.lower() != "magnet":
        return None
    for xt in parse_qs(parsed.query).get("xt", []):
        if not xt.lower().startswith("urn:btih:"):
            continue
        value = xt[9:].strip()
        if _HEX40.match(value):
            return value.lower()
        if _B32.match(value.upper()):
            return base64.b32decode(value.upper()).hex()
    return None


def normalize_info_hash(value: Optional[str]) -> Optional[str]:
    """Return a lowercase 40-hex info-hash, or None for anything else."""
    v = (value or "").strip()
    if _HEX40.match(v):
        return v.lower()
    if _B32.match(v.upper()):
        return base64.b32decode(v.upper()).hex()
    return None


# ---------------------------------------------------------------------------
# Picking the file to play
# ---------------------------------------------------------------------------

def pick_stream_file(files: Sequence[Dict[str, Any]]) -> Optional[Tuple[int, Dict[str, Any]]]:
    """Pick the main video in a torrent's file list (qBittorrent /torrents/files).

    Returns (position in torrent order, file entry): the largest non-sample video.
    """
    best: Optional[Tuple[Tuple[int, int], int, Dict[str, Any]]] = None
    for pos, f in enumerate(files):
        name = str(f.get("name") or "")
        lower = name.lower()
        if not lower.endswith(VIDEO_EXTENSIONS):
            continue
        not_sample = 0 if "sample" in lower.rsplit("/", 1)[-1] else 1
        score = (not_sample, int(f.get("size") or 0))
        if best is None or score > best[0]:
            best = (score, pos, f)
    if best is None:
        return None
    return best[1], best[2]


def file_offset(files: Sequence[Dict[str, Any]], pos: int) -> int:
    """Byte offset of files[pos] inside the torrent's concatenated payload."""
    return sum(int(f.get("size") or 0) for f in files[:pos])


# ---------------------------------------------------------------------------
# Piece maths
# ---------------------------------------------------------------------------

def available_end(
    start: int,
    end: int,
    offset: int,
    piece_size: int,
    piece_states: Sequence[int],
) -> Optional[int]:
    """Clamp a requested file byte range to what is already downloaded.

    ``start``/``end`` are inclusive file positions, ``offset`` is the file's
    position in the torrent. Returns the last byte that can be served from
    ``start`` onwards without crossing a missing piece, or None when the piece
    holding ``start`` itself isn't downloaded yet.
    """
    if piece_size <= 0 or start > end:
        return None
    first = (offset + start) // piece_size
    last = (offset + end) // piece_size
    if first >= len(piece_states) or piece_states[first] != PIECE_HAVE:
        return None
    p = first
    while p <= last and p < len(piece_states) and piece_states[p] == PIECE_HAVE:
        p += 1
    if p > last:
        return end
    # p is the first missing piece; serve up to the byte before it.
    return min(end, p * piece_size - offset - 1)


def contiguous_fraction(size: int, offset: int, piece_size: int, piece_states: Sequence[int]) -> float:
    """Share of the file (0..1) that is playable from the start without a gap."""
    if size <= 0 or piece_size <= 0:
        return 0.0
    end = available_end(0, size - 1, offset, piece_size, piece_states)
    if end is None:
        return 0.0
    return round((end + 1) / size, 4)


def describe_states(piece_states: List[int]) -> str:
    """Tiny summary for logs: '120/800 pieces'."""
    have = sum(1 for s in piece_states if s == PIECE_HAVE)
    return f"{have}/{len(piece_states)} pieces"
