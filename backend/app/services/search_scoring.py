"""Deterministic Weighted Sum Model (WSM) scoring, deduplication, and RRF deep-search fusion."""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.app.config import settings

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_RES_2160_RE = re.compile(r"\b(?:2160p|4k|uhd)\b", re.IGNORECASE)
_RES_1080_RE = re.compile(r"\b(?:1080p|fhd)\b", re.IGNORECASE)
_HDR_DV_RE = re.compile(r"\b(?:2160p|4k|uhd|hdr10?\+?|dv|dovi|dolby\s*vision)\b", re.IGNORECASE)
_GOOD_CODEC_RE = re.compile(r"(?:\.mp4\b|\bmp4\b|\bweb-?dl\b|\bwebdl\b|\baac\b|\be-?ac3\b|\beac-?3\b)", re.IGNORECASE)
_BAD_CONTAINER_RE = re.compile(r"(?:\.(?:avi|wmv|iso)\b|\b(?:avi|wmv|iso)\b)", re.IGNORECASE)
_HEAVY_REMUX_RE = re.compile(r"\b(?:remux|truehd|dts-?hd|dtshd)\b", re.IGNORECASE)
_RELEASE_NOISE_TAIL_RE = re.compile(
    r"\b(?:2160p|1080p|720p|480p|4k|uhd|remux|blu-?ray|bdrip|web-?dl|webrip|hdtv|"
    r"x264|x265|h\.?264|h\.?265|hevc|av1|hdr10?\+?|dv|dovi|atmos|truehd|dts-?hd|aac|eac3)\b.*$",
    re.IGNORECASE,
)

_FILLER_WORDS = frozenset({
    "the", "a", "an", "of", "and", "in", "on", "at", "to", "for", "with", "by",
})

_BASE_ADULT_MARKER_RE = re.compile(r"(?:whisparr|adult|\bxxx\b)", re.IGNORECASE)
_BASE_ADULT_SCRUB_RE = re.compile(r"(?:whisparr|\bxxx\b)", re.IGNORECASE)


def get_private_trackers() -> frozenset[str]:
    """Return configured private tracker/indexer names (lowercase)."""
    return frozenset(
        tok.strip().lower()
        for tok in (settings.PRIVATE_TRACKERS or "").split(",")
        if tok.strip()
    )


def get_adult_indexer_keywords() -> Tuple[str, ...]:
    """Return configured custom adult indexer/domain keywords (lowercase)."""
    return tuple(
        tok.strip().lower()
        for tok in (settings.ADULT_INDEXER_KEYWORDS or "").split(",")
        if tok.strip()
    )


def matches_adult_marker(value: str, *, include_adult_word: bool = True) -> bool:
    """Return True if value matches base adult tokens or configured adult keywords."""
    text = value or ""
    if not text:
        return False
    base_re = _BASE_ADULT_MARKER_RE if include_adult_word else _BASE_ADULT_SCRUB_RE
    if base_re.search(text):
        return True
    lowered = text.lower()
    return any(kw in lowered for kw in get_adult_indexer_keywords())


def scrub_adult_tokens(value: str) -> str:
    """Redact base adult tokens and configured adult keywords before external prompts."""
    text = _BASE_ADULT_SCRUB_RE.sub("[Vault]", value or "")
    keywords = get_adult_indexer_keywords()
    if not keywords:
        return text
    pattern = re.compile("|".join(re.escape(kw) for kw in keywords), re.IGNORECASE)
    return pattern.sub("[Vault]", text)

_GAUSSIAN_PROFILES: Dict[Tuple[str, str], Tuple[float, float]] = {
    ("movie", "2160p"): (20.0, 11.0),
    ("movie", "1080p"): (8.0, 4.5),
    ("tv", "2160p"): (4.5, 2.5),
    ("tv", "1080p"): (2.2, 1.4),
    ("adult", "2160p"): (4.0, 3.0),
    ("adult", "1080p"): (2.5, 1.8),
}


def _to_ascii_lower(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    return decomposed.encode("ascii", "ignore").decode("ascii").lower()


def _normalize_title(title: str) -> str:
    """Normalize a release or media title to clean ASCII words for dedup matching."""
    ascii_title = _to_ascii_lower(title)
    base = _RELEASE_NOISE_TAIL_RE.split(ascii_title, maxsplit=1)[0]
    cleaned = _NON_ALNUM_RE.sub(" ", base).strip()
    return cleaned or _NON_ALNUM_RE.sub(" ", ascii_title).strip()


def _tokenize(text: str) -> List[str]:
    return [tok for tok in _NON_ALNUM_RE.sub(" ", _to_ascii_lower(text)).split() if tok]


def _normalize_category_bucket(category: str, item: Optional[Dict[str, Any]] = None) -> str:
    cat = (category or "").strip().lower()
    if cat in {"adult", "xxx"}:
        return "adult"
    if cat in {"tv", "tv shows", "tv series", "series"}:
        return "tv"
    if cat in {"movie", "movies", "film"}:
        return "movie"

    if item:
        if item.get("is_adult"):
            return "adult"
        cat_id = item.get("category_id")
        if isinstance(cat_id, int):
            if 6000 <= cat_id < 7000:
                return "adult"
            if 5000 <= cat_id < 6000:
                return "tv"
        item_cat = str(item.get("category") or "").lower()
        if "xxx" in item_cat or "adult" in item_cat:
            return "adult"
        if "tv" in item_cat:
            return "tv"
    return "movie"


def _detect_res_bucket(title: str) -> str:
    if _RES_2160_RE.search(title or ""):
        return "2160p"
    return "1080p"


def _is_private_tracker(indexer_name: str, item: Optional[Dict[str, Any]] = None) -> bool:
    if item and item.get("is_private") is not None:
        return bool(item["is_private"])
    return (indexer_name or "").strip().lower() in get_private_trackers()


def _get_magnet(item: Dict[str, Any]) -> Optional[str]:
    return item.get("magnetUrl") or item.get("magnet_url")


def _get_download(item: Dict[str, Any]) -> Optional[str]:
    return item.get("downloadUrl") or item.get("download_url")


def _get_info_hash(item: Dict[str, Any]) -> str:
    return str(item.get("infoHash") or item.get("info_hash") or "").strip()


def _fallback_title_size_key(item: Dict[str, Any]) -> str:
    size_bytes = float(item.get("size") or 0)
    return f"{_normalize_title(str(item.get('title') or ''))}_{round(size_bytes / 1048576)}"


def _build_dedup_key(item: Dict[str, Any]) -> str:
    """Build deterministic deduplication key via >=32-char infoHash or (normalized title + MiB size)."""
    info_hash = _get_info_hash(item)
    if len(info_hash) >= 32:
        return info_hash.lower()
    return _fallback_title_size_key(item)


def _resolve_bucket_key(
    item: Dict[str, Any],
    buckets: Dict[str, Dict[str, Any]],
    fallback_index: Dict[str, str],
) -> str:
    """Match by infoHash first, bridging across missing infoHash via normalized (title, size_mb)."""
    primary = _build_dedup_key(item)
    if primary in buckets:
        return primary

    has_hash = len(_get_info_hash(item)) >= 32
    fb_key = _fallback_title_size_key(item)
    existing_key = fallback_index.get(fb_key)
    if existing_key and existing_key in buckets:
        existing_has_hash = len(_get_info_hash(buckets[existing_key])) >= 32
        # Bridge only when at least one side lacks a canonical >=32-char infoHash
        if not (has_hash and existing_has_hash):
            return existing_key

    return primary


def _compute_relevance(query_tokens: List[str], title: str) -> float:
    if not query_tokens:
        return 1.0
    title_tokens = set(_tokenize(title))
    if "4k" in title_tokens or "uhd" in title_tokens:
        title_tokens.add("2160p")
    if "2160p" in title_tokens or "uhd" in title_tokens:
        title_tokens.add("4k")

    q_set = set(query_tokens)
    matched = len(q_set & title_tokens)
    return matched / len(q_set)


def _compute_wsm_components(
    item: Dict[str, Any],
    relevance: float,
    category: str,
    merged_seeders: Optional[int] = None,
) -> float:
    """Compute calibrated [0.0, 1.0] Weighted Sum Model score."""
    title = str(item.get("title") or "")
    seeders = max(0, int(merged_seeders if merged_seeders is not None else (item.get("seeders") or 0)))
    leechers = max(0, int(item.get("leechers") or 0))
    size_bytes = max(0, int(item.get("size") or 0))
    indexer = str(item.get("indexer") or "Unknown")

    # 1. Swarm health (0.20 + 0.10 = 0.30)
    seeder_health = min(1.0, math.log1p(seeders) / math.log1p(150))
    ratio_health = (seeders + 1.0) / (seeders + leechers + 1.0)

    # 2. Dynamic Gaussian size (0.15)
    bucket = _normalize_category_bucket(category, item)
    res_bucket = _detect_res_bucket(title)
    mu, sigma = _GAUSSIAN_PROFILES.get((bucket, res_bucket), (8.0, 4.5))
    if size_bytes > 0:
        size_gb = size_bytes / (1024 ** 3)
        size_score = math.exp(-0.5 * ((size_gb - mu) / sigma) ** 2)
    else:
        size_score = 0.25

    # 3. Calibrated Quality & Browser Direct-Play compatibility (0.15)
    compat = 0.60
    bad_container = bool(_BAD_CONTAINER_RE.search(title))
    if _GOOD_CODEC_RE.search(title):
        compat += 0.25
    if not bad_container and _HDR_DV_RE.search(title):
        compat += 0.15
    if _HEAVY_REMUX_RE.search(title):
        compat -= 0.30
    if bad_container:
        compat -= 0.55
        # Do not let an unplayable legacy container (.avi/.wmv/.iso) win on ideal size.
        size_score *= 0.25
    compat = max(0.0, min(1.0, compat))

    # 4. Infrastructure & Private Tracker reliability (0.15)
    infra = 0.0
    if _is_private_tracker(indexer, item):
        infra += 0.65
    if _get_magnet(item) or _get_download(item):
        infra += 0.35
    infra = max(0.0, min(1.0, infra))

    wsm = (
        0.25 * max(0.0, min(1.0, relevance))
        + 0.20 * seeder_health
        + 0.10 * ratio_health
        + 0.15 * size_score
        + 0.15 * compat
        + 0.15 * infra
    )
    return max(0.0, min(1.0, wsm))


def _has_preview_meta(item: Dict[str, Any]) -> bool:
    return bool(item.get("preview_indexer") and (item.get("info_url") or item.get("poster_url")))


def _copy_preview_meta(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    target["preview_indexer"] = source["preview_indexer"]
    target["info_url"] = source.get("info_url")
    target["poster_url"] = source.get("poster_url")


def _set_promoted_source(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    """Promote source metadata fields onto the canonical merged release."""
    indexer = str(source.get("indexer") or "Unknown")
    target["best_source"] = indexer
    target["indexer"] = indexer
    if "indexerId" in source or "indexer_id" in source:
        idx_id = source.get("indexerId", source.get("indexer_id"))
        target["indexerId"] = idx_id
        target["indexer_id"] = idx_id
    if source.get("guid"):
        target["guid"] = source["guid"]

    ih = _get_info_hash(source)
    if ih and not _get_info_hash(target):
        target["info_hash"] = ih
        if "infoHash" in source or "infoHash" in target:
            target["infoHash"] = ih

    mag = _get_magnet(source)
    if mag:
        target["magnet_url"] = mag
        if "magnetUrl" in source or "magnetUrl" in target:
            target["magnetUrl"] = mag
    dl = _get_download(source)
    if dl:
        target["download_url"] = dl
        if "downloadUrl" in source or "downloadUrl" in target:
            target["downloadUrl"] = dl
    if _has_preview_meta(source):
        _copy_preview_meta(target, source)


def _backfill_links(target: Dict[str, Any], donor: Dict[str, Any]) -> None:
    ih = _get_info_hash(donor)
    if ih and not _get_info_hash(target):
        target["info_hash"] = ih
        if "infoHash" in donor or "infoHash" in target:
            target["infoHash"] = ih
    if not _get_magnet(target) and _get_magnet(donor):
        mag = _get_magnet(donor)
        target["magnet_url"] = mag
        if "magnetUrl" in donor:
            target["magnetUrl"] = mag
    if not _get_download(target) and _get_download(donor):
        dl = _get_download(donor)
        target["download_url"] = dl
        if "downloadUrl" in donor:
            target["downloadUrl"] = dl
    if not _has_preview_meta(target) and _has_preview_meta(donor):
        _copy_preview_meta(target, donor)


def deduplicate_and_score_results(
    query: str,
    raw_results: List[Dict[str, Any]],
    category: str = "movie",
) -> List[Dict[str, Any]]:
    """Filter by normalized relevance, deduplicate across trackers cleanly, and score via WSM."""
    if not raw_results:
        return []

    query_tokens = _tokenize(query)
    scored_pairs = []
    for item in raw_results:
        rel = _compute_relevance(query_tokens, str(item.get("title") or ""))
        scored_pairs.append((item, rel))

    relevant = [(it, rel) for it, rel in scored_pairs if rel >= 0.55]
    working = relevant if relevant else scored_pairs

    buckets: Dict[str, Dict[str, Any]] = {}
    fallback_index: Dict[str, str] = {}
    meta: Dict[str, Dict[str, Any]] = {}

    for raw_item, rel in working:
        key = _resolve_bucket_key(raw_item, buckets, fallback_index)
        fb_key = _fallback_title_size_key(raw_item)
        item = dict(raw_item)
        indexer = str(item.get("indexer") or "Unknown")
        is_priv = _is_private_tracker(indexer, item)
        item_seeders = max(0, int(item.get("seeders") or 0))
        item_wsm = _compute_wsm_components(item, rel, category)

        if key not in buckets:
            item["sources"] = list(dict.fromkeys([*(item.get("sources") or []), indexer]))
            item["best_source"] = str(item.get("best_source") or indexer)
            item["muuvies_score"] = round(item_wsm * 100.0, 1)
            item["quality_score"] = int(round(item["muuvies_score"]))
            buckets[key] = item
            fallback_index.setdefault(fb_key, key)
            meta[key] = {
                "rel": rel,
                "best_wsm": item_wsm,
                "best_is_private": is_priv,
                "public_max_seeders": 0 if is_priv else item_seeders,
                "private_seeders": {indexer.lower(): item_seeders} if is_priv else {},
            }
            continue

        existing = buckets[key]
        fallback_index.setdefault(fb_key, key)
        m = meta[key]
        if indexer not in existing["sources"]:
            existing["sources"].append(indexer)

        if is_priv:
            prev_priv = m["private_seeders"].get(indexer.lower(), 0)
            m["private_seeders"][indexer.lower()] = max(prev_priv, item_seeders)
        else:
            m["public_max_seeders"] = max(m["public_max_seeders"], item_seeders)

        merged_seeders = m["public_max_seeders"] + sum(m["private_seeders"].values())
        existing["seeders"] = merged_seeders
        existing["leechers"] = max(int(existing.get("leechers") or 0), int(item.get("leechers") or 0))

        should_promote = (is_priv and not m["best_is_private"]) or (
            is_priv == m["best_is_private"] and item_wsm > m["best_wsm"]
        )
        if should_promote:
            _set_promoted_source(existing, item)
            m["best_is_private"] = is_priv
            m["best_wsm"] = item_wsm
        else:
            _backfill_links(existing, item)

        m["rel"] = max(m["rel"], rel)
        recomputed = _compute_wsm_components(existing, m["rel"], category, merged_seeders=merged_seeders)
        existing["muuvies_score"] = round(recomputed * 100.0, 1)
        existing["quality_score"] = int(round(existing["muuvies_score"]))

    return sorted(
        buckets.values(),
        key=lambda x: (float(x.get("muuvies_score") or 0.0), int(x.get("seeders") or 0)),
        reverse=True,
    )


_TECH_QUALIFIER_RE = re.compile(
    r"\b(?:2160p|4k|uhd|1080p|fhd|720p|480p|web-?dl|webdl|webrip|bluray|blu-?ray|"
    r"remux|x265|x264|h\.?265|h\.?264|hevc|hdr10?\+?|dv|dovi|mp4|mkv)\b",
    re.IGNORECASE,
)
_ALIAS_PUNCT_RE = re.compile(r"[:;|/]+")


def expand_deep_queries(
    query: str,
    category: str = "All",
    library_items: Optional[Sequence[Dict[str, Any]]] = None,
    local_aliases: Optional[Sequence[str]] = None,
) -> List[str]:
    """Build up to 3 complementary search queries for Deep Search fanout."""
    raw = (query or "").strip()
    if not raw:
        return []

    ascii_norm = re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii"),
    ).strip()
    primary = ascii_norm or raw
    variants: List[str] = [primary]

    cat_bucket = _normalize_category_bucket(category)
    year_match = _YEAR_RE.search(primary)

    # 2. Best local alias / studio / original title from FTS5, *arr, or grab_history
    alias_candidates: List[str] = [str(a) for a in (local_aliases or ()) if a]
    for lib in library_items or ():
        lib_title = str(lib.get("title") or "").strip()
        if not lib_title:
            continue
        lib_year = lib.get("year")
        if lib_year and str(lib_year) not in primary and int(lib_year) > 1900:
            alias_candidates.append(f"{lib_title} {lib_year}".strip())
        alias_candidates.append(lib_title)

    for cand in alias_candidates:
        cleaned_alias = _ALIAS_PUNCT_RE.sub(" ", cand)
        cleaned_alias = re.sub(r"\s+", " ", unicodedata.normalize("NFKD", cleaned_alias).encode("ascii", "ignore").decode("ascii")).strip()
        if len(cleaned_alias) >= 3 and cleaned_alias.lower() != primary.lower():
            variants.append(cleaned_alias)
            break

    # 3. Relaxed query (without technical qualifiers / year) or resolution complement
    relaxed = _TECH_QUALIFIER_RE.sub(" ", primary)
    relaxed = _YEAR_RE.sub(" ", relaxed)
    relaxed = re.sub(r"\s+", " ", relaxed).strip()
    if len(relaxed) >= 3 and relaxed.lower() not in {v.lower() for v in variants}:
        variants.append(relaxed)
    elif _RES_2160_RE.search(primary) and "4k" in primary.lower():
        variants.append(_RES_2160_RE.sub("2160p", primary))
    elif cat_bucket in ("movie", "tv") and not _RES_1080_RE.search(primary):
        variants.append(f"{primary} 2160p")

    tokens = _tokenize(primary)
    stripped_tokens = [t for t in tokens if t not in _FILLER_WORDS]
    if stripped_tokens and " ".join(stripped_tokens) not in {v.lower() for v in variants}:
        variants.append(" ".join(stripped_tokens))

    deduped: List[str] = []
    seen = set()
    for v in variants:
        cleaned = re.sub(r"\s+", " ", v).strip()
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            deduped.append(cleaned)
        if len(deduped) == 3:
            break
    return deduped


def fuse_deep_search_lists(
    ranked_lists: Iterable[List[Dict[str, Any]]],
    k: int = 60,
    wsm_weight: float = 0.35,
) -> List[Dict[str, Any]]:
    """Fuse multiple scored lists via Reciprocal Rank Fusion blended with WSM quality."""
    non_empty = [lst for lst in ranked_lists if lst]
    if not non_empty:
        return []

    rrf_scores: Dict[str, float] = {}
    fused_items: Dict[str, Dict[str, Any]] = {}
    fallback_index: Dict[str, str] = {}
    fused_meta: Dict[str, Tuple[bool, float]] = {}

    for lst in non_empty:
        for rank, raw_item in enumerate(lst):
            key = _resolve_bucket_key(raw_item, fused_items, fallback_index)
            fb_key = _fallback_title_size_key(raw_item)
            rrf_scores[key] = rrf_scores.get(key, 0.0) + (1.0 / (k + rank + 1))

            item = dict(raw_item)
            indexer = str(item.get("indexer") or "Unknown")
            is_priv = _is_private_tracker(indexer, item)
            item_wsm = float(item.get("muuvies_score") or item.get("quality_score") or 50.0)

            if key not in fused_items:
                item["sources"] = list(dict.fromkeys([*(item.get("sources") or []), indexer]))
                item["best_source"] = str(item.get("best_source") or indexer)
                fused_items[key] = item
                fallback_index.setdefault(fb_key, key)
                fused_meta[key] = (is_priv, item_wsm)
                continue

            existing = fused_items[key]
            fallback_index.setdefault(fb_key, key)
            for src in [*(item.get("sources") or []), indexer]:
                if src and src not in existing["sources"]:
                    existing["sources"].append(src)

            existing["seeders"] = max(int(existing.get("seeders") or 0), int(item.get("seeders") or 0))
            existing["leechers"] = max(int(existing.get("leechers") or 0), int(item.get("leechers") or 0))

            prev_priv, prev_wsm = fused_meta[key]
            if (is_priv and not prev_priv) or (is_priv == prev_priv and item_wsm > prev_wsm):
                _set_promoted_source(existing, item)
                fused_meta[key] = (is_priv, max(prev_wsm, item_wsm))
            else:
                _backfill_links(existing, item)

    max_possible_rrf = len(non_empty) / (k + 1)
    for key, item in fused_items.items():
        rrf_norm = min(1.0, rrf_scores[key] / max_possible_rrf) if max_possible_rrf > 0 else 0.0
        wsm_norm = min(1.0, max(0.0, fused_meta[key][1] / 100.0))
        combined = (1.0 - wsm_weight) * rrf_norm + wsm_weight * wsm_norm
        item["muuvies_score"] = round(combined * 100.0, 1)
        item["quality_score"] = int(round(item["muuvies_score"]))

    return sorted(
        fused_items.values(),
        key=lambda x: (float(x.get("muuvies_score") or 0.0), int(x.get("seeders") or 0)),
        reverse=True,
    )
