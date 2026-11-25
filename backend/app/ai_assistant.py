import asyncio
import hashlib
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import sentry_sdk
from google import genai
from google.genai import errors as genai_errors, types

from backend.app.config import settings
from backend.app.schemas import QualityBadge, SmartRankingSummary
from backend.app.services.cache import curator_cache
from backend.app.services.http_clients import get_search_client
from backend.app.services.search_scoring import matches_adult_marker, scrub_adult_tokens

logger = logging.getLogger("muuvies.ai_assistant")

REPUTABLE_GROUPS = {
    "framestor", "playbd", "flux", "ntb", "cmrg", "don", "ctrlhd",
    "kogi", "tigole", "qxr", "epsilon", "d-z0n", "kings", "monkee",
    "decibeel", "bhd", "hdb", "chd", "wiki", "tayto", "hifi",
}


def parse_release_attributes(title: str) -> Dict[str, Any]:
    """Extract resolution, source, HDR, audio codec, and release group from a torrent title."""
    t_lower = title.lower()
    badges: List[QualityBadge] = []
    tags: List[str] = []

    # Resolution
    res = None
    if "2160p" in t_lower or "4k" in t_lower or "uhd" in t_lower:
        res = "4K 2160p"
        badges.append(QualityBadge(label="4K UHD", color="purple"))
        tags.append("4K UHD")
    elif "1080p" in t_lower or "fhd" in t_lower:
        res = "1080p"
        badges.append(QualityBadge(label="1080p", color="blue"))
        tags.append("1080p")
    elif "720p" in t_lower or "hd" in t_lower:
        res = "720p"
        badges.append(QualityBadge(label="720p", color="gray"))
        tags.append("720p")

    # Source
    source = "Unknown"
    if "remux" in t_lower:
        source = "REMUX"
        badges.append(QualityBadge(label="REMUX", color="green"))
        tags.append("REMUX")
    elif "bluray" in t_lower or "blu-ray" in t_lower or "bdrip" in t_lower:
        source = "BluRay"
        badges.append(QualityBadge(label="BluRay", color="blue"))
        tags.append("BluRay")
    elif "web-dl" in t_lower or "webdl" in t_lower:
        source = "WEB-DL"
        badges.append(QualityBadge(label="WEB-DL", color="cyan"))
        tags.append("WEB-DL")
    elif "webrip" in t_lower:
        source = "WEBRip"
        badges.append(QualityBadge(label="WEBRip", color="yellow"))
        tags.append("WEBRip")
    elif "cam" in t_lower or "telesync" in t_lower or re.search(r"\bts\b", t_lower):
        source = "CAM/TS"
        badges.append(QualityBadge(label="CAM / Low Quality", color="red"))
        tags.append("CAM/TS")

    # HDR / Color
    has_dv = "dv" in t_lower or "dovi" in t_lower or "dolby vision" in t_lower
    has_hdr = "hdr" in t_lower
    if has_dv:
        badges.append(QualityBadge(label="Dolby Vision", color="purple"))
        tags.append("DV")
    if "hdr10+" in t_lower:
        badges.append(QualityBadge(label="HDR10+", color="yellow"))
        tags.append("HDR10+")
    elif has_hdr:
        badges.append(QualityBadge(label="HDR", color="yellow"))
        tags.append("HDR")

    # Audio
    has_premium_audio = False
    if "atmos" in t_lower:
        badges.append(QualityBadge(label="Atmos", color="indigo"))
        tags.append("Atmos")
        has_premium_audio = True
    elif "truehd" in t_lower:
        badges.append(QualityBadge(label="TrueHD", color="indigo"))
        tags.append("TrueHD")
        has_premium_audio = True
    elif "dts-hd" in t_lower:
        badges.append(QualityBadge(label="DTS-HD", color="indigo"))
        tags.append("DTS-HD")
        has_premium_audio = True

    # Group detection
    group_match = re.search(r"-([A-Za-z0-9_]+)(?:\[.*?\])?$", title.strip())
    detected_group = group_match.group(1).lower() if group_match else None
    if detected_group and detected_group in REPUTABLE_GROUPS:
        p2p_tag = f"P2P:{detected_group.upper()}"
        badges.append(QualityBadge(label=f"P2P: {detected_group.upper()}", color="green"))
        tags.append(p2p_tag)

    return {
        "resolution": res,
        "source": source,
        "badges": badges,
        "tags": tags,
        "group": detected_group,
        "has_dv": has_dv,
        "has_hdr": has_hdr,
        "has_premium_audio": has_premium_audio,
    }


def find_library_match(title: str, library_items: List[Dict[str, Any]]) -> Tuple[bool, Optional[str], Optional[str]]:
    """Return (in_library, status_note, canonical_stream_id) for an *arr library match."""
    clean_title = re.sub(r"[^a-zA-Z0-9 ]", "", title.lower())
    for item in library_items:
        lib_title = item.get("title", "")
        clean_lib = re.sub(r"[^a-zA-Z0-9 ]", "", lib_title.lower())
        if clean_lib and (clean_lib in clean_title or clean_title in clean_lib):
            status_note = f"Already in library: '{lib_title}'"
            if item.get("has_file"):
                status_note += " (File on disk)"
            svc, ext_id = item.get("service"), item.get("id")
            stream_id = f"{svc}:{ext_id}" if (svc and ext_id is not None) else (str(ext_id) if ext_id is not None else None)
            return True, status_note, stream_id
    return False, None, None


def check_library_duplicate(title: str, library_items: List[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
    """Check if a candidate release matches an item in the user's *arr library mirror."""
    matched, note, _ = find_library_match(title, library_items)
    return matched, note


def is_adult_item(item: Dict[str, Any]) -> bool:
    """Return True if a release, library entry, or torrent belongs to the Adult Vault."""
    if not isinstance(item, dict):
        return False
    if item.get("is_adult"):
        return True
    if str(item.get("service") or "").strip().lower() == "whisparr":
        return True
    cat_id = item.get("category_id")
    if isinstance(cat_id, int) and 6000 <= cat_id < 7000:
        return True
    for field in ("category", "indexer", "tracker"):
        val = str(item.get(field) or "")
        if val and matches_adult_marker(val):
            return True
    for src in item.get("sources") or []:
        if matches_adult_marker(str(src or "")):
            return True
    return False


def _scrub_adult_text(value: str) -> str:
    return scrub_adult_tokens(value or "")


SYSTEM_PROMPT = (
    "You are an expert seedbox release curator and media preservation specialist. "
    "Evaluate the compressed top 8 candidate releases based on bitrate/source quality "
    "(4K REMUX > BluRay > WEB-DL >> CAM), reputable P2P groups (Framestor, playBD, FLUX, NTb, CMRG), "
    "HDR/DV/Atmos tags, and seeder health. "
    "Select the single optimal release and provide a concise 1-2 sentence justification. "
    "Return strictly valid JSON matching this schema:\n"
    "{\n"
    '  "query_intent": "string summary of user preferences",\n'
    '  "recommended_title": "exact title of the best release from Candidate Releases",\n'
    '  "recommendation_reason": "concise explanation of why this is the best pick",\n'
    '  "ratings": [\n'
    '    {"title": "exact release title", "score": 85, "note": "concise note"}\n'
    "  ]\n"
    "}"
)


class MultiProviderReleaseAssistant:
    def __init__(self):
        self.gemini_model = settings.GEMINI_MODEL
        self.github_model = settings.GITHUB_MODEL
        self._gemini_cooldown_until: float = 0.0
        self._gemini_cooldown_seconds: float = 60.0
        self._gemini_client: Optional[genai.Client] = None
        self._gemini_client_key: str = ""

    def _record_gemini_failure(
        self,
        model: str,
        err: Exception,
        status_code: Optional[int] = None,
    ) -> bool:
        """Log Gemini failure, add Sentry breadcrumb, and trigger short cooldown on 503/429."""
        err_text = str(err)
        err_upper = err_text.upper()
        is_capacity_or_rate = (
            status_code in (429, 503)
            or "503" in err_text
            or "429" in err_text
            or "UNAVAILABLE" in err_upper
            or "RESOURCE_EXHAUSTED" in err_upper
        )
        logger.warning("Gemini generation error with %s (code=%s): %s", model, status_code, err)
        sentry_sdk.add_breadcrumb(
            category="ai.curator",
            message=f"Gemini fallback triggered ({model})",
            level="warning",
            data={"model": model, "status_code": status_code, "error": err_text[:200]},
        )
        if is_capacity_or_rate:
            self._gemini_cooldown_until = time.monotonic() + self._gemini_cooldown_seconds
            logger.info(
                "Activated %.0fs Gemini circuit-breaker cooldown; cascading to Azure OpenAI / fallback",
                self._gemini_cooldown_seconds,
            )
            return True
        return False

    def _get_gemini_client(self) -> Optional[genai.Client]:
        api_key = (settings.GEMINI_API_KEY or "").strip()
        if not api_key:
            return None
        if self._gemini_client is not None and self._gemini_client_key == api_key:
            return self._gemini_client
        try:
            self._gemini_client = genai.Client(api_key=api_key)
            self._gemini_client_key = api_key
            return self._gemini_client
        except Exception:
            return None

    def enrich_and_score_heuristically(
        self,
        query: str,
        releases: List[Dict[str, Any]],
        library_items: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], Optional[SmartRankingSummary]]:
        """Synchronously score and badge releases in 0ms without blocking on LLM inference."""
        if not releases:
            return [], None

        wants_4k = bool(re.search(r"\b(?:4k|2160p|uhd)\b", query or "", re.IGNORECASE))

        enriched: List[Dict[str, Any]] = []
        for r in releases:
            item = dict(r)
            attrs = parse_release_attributes(item["title"])
            item_badges = list(attrs["badges"])

            in_lib, lib_note, lib_stream_id = find_library_match(item["title"], library_items)
            item["in_library"] = in_lib
            item["library_item_title"] = lib_note
            item["library_item_id"] = lib_stream_id
            if in_lib:
                item_badges.insert(0, QualityBadge(label="In Library", color="yellow"))

            score = 50
            if attrs["source"] == "REMUX":
                score += 30
            elif attrs["source"] == "BluRay":
                score += 20
            elif attrs["source"] == "WEB-DL":
                score += 15
            elif attrs["source"] == "CAM/TS":
                score -= 40

            if attrs["resolution"] == "4K 2160p":
                score += 20 if wants_4k else 5
            elif wants_4k:
                score -= 10
            elif attrs["resolution"] == "1080p":
                score += 3

            if attrs["has_dv"] or attrs["has_hdr"]:
                score += 4
            if attrs["has_premium_audio"]:
                score += 3
            if attrs.get("group") in REPUTABLE_GROUPS:
                score += 10

            seeders = item.get("seeders", 0)
            if seeders > 20:
                score += 10
            elif seeders == 0:
                score -= 25

            if item.get("muuvies_score") is not None:
                m_score = float(item["muuvies_score"])
                item["muuvies_score"] = round(m_score, 1)
                item["quality_score"] = max(1, min(100, int(round(m_score))))
            else:
                item["quality_score"] = max(1, min(100, score))
            item["badges"] = item_badges
            item["ai_pick"] = False
            item["ai_analysis"] = None
            enriched.append(item)

        sorted_by_score = sorted(
            enriched,
            key=lambda x: (
                float(x.get("muuvies_score") if x.get("muuvies_score") is not None else x.get("quality_score", 0)),
                x.get("seeders", 0),
            ),
            reverse=True,
        )
        summary: Optional[SmartRankingSummary] = None
        if sorted_by_score:
            top_pick = sorted_by_score[0]
            top_pick["ai_pick"] = True
            score_label = (
                f"WSM {top_pick['muuvies_score']}"
                if top_pick.get("muuvies_score") is not None
                else "heuristic scoring"
            )
            reason = (
                f"Ranked #1 by {score_label}: {top_pick.get('size_formatted', '')} "
                f"release with {top_pick.get('seeders', 0)} seeders on {top_pick.get('indexer', 'Unknown')}."
            )
            top_pick["ai_analysis"] = reason
            summary = SmartRankingSummary(
                ai_active=False,
                query_intent="Deterministic WSM + Heuristic Ranking",
                recommended_guid=top_pick.get("guid"),
                recommended_title=top_pick.get("title"),
                recommendation_reason=reason,
                model_used="Heuristic Rules Engine",
            )

        return enriched, summary

    def compress_candidates_for_llm(
        self,
        releases: List[Dict[str, Any]],
        top_n: int = 15,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], str]:
        """Pre-filter releases down to top_n candidates with minimal LLM payload fields."""
        if not releases:
            return [], [], ""

        def _score_key(r: Dict[str, Any]) -> Tuple[float, int]:
            m_score = r.get("muuvies_score")
            if m_score is not None:
                return (float(m_score), int(r.get("seeders", 0)))
            q_score = r.get("quality_score")
            if q_score is None:
                attrs = parse_release_attributes(r.get("title", ""))
                q_score = 80 if attrs["source"] == "REMUX" else 65 if attrs["source"] == "BluRay" else 50
            return (float(q_score), int(r.get("seeders", 0)))

        top_originals = sorted(releases, key=_score_key, reverse=True)[:top_n]
        compressed: List[Dict[str, Any]] = []
        hash_inputs: List[Dict[str, Any]] = []

        for r in top_originals:
            title = str(r.get("title", ""))
            raw_badges = r.get("badges") or []
            tags: List[str] = []
            for b in raw_badges:
                if isinstance(b, QualityBadge):
                    tags.append(b.label)
                elif isinstance(b, dict) and b.get("label"):
                    tags.append(str(b["label"]))
                elif isinstance(b, str):
                    tags.append(b)
            if not tags:
                tags = parse_release_attributes(title)["tags"]

            size_bytes = int(r.get("size") or 0)
            size_gb = round(size_bytes / (1024**3), 2) if size_bytes > 0 else 0.0
            entry = {
                "title": title,
                "size_gb": size_gb,
                "seeders": int(r.get("seeders", 0)),
                "indexer": str(r.get("indexer", "Unknown")),
                "tags": tags,
            }
            compressed.append(entry)
            hash_inputs.append({
                "guid": str(r.get("guid", "")),
                "title": title,
                "size_gb": size_gb,
            })

        digest = hashlib.sha256(
            json.dumps(hash_inputs, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        return top_originals, compressed, digest

    def _build_llm_user_content(
        self,
        query: str,
        candidates_summary: List[Dict[str, Any]],
        library_items: List[Dict[str, Any]],
        user_content_override: Optional[str] = None,
    ) -> str:
        if user_content_override:
            return user_content_override
        return (
            f'User Search Query: "{query}"\n\n'
            f"Candidate Releases:\n{json.dumps(candidates_summary, separators=(',', ':'))}\n\n"
            f"Existing Library Items:\n{json.dumps([it.get('title') for it in library_items[:8]])}"
        )

    async def _call_gemini(
        self,
        query: str,
        candidates_summary: List[Dict[str, Any]],
        library_items: List[Dict[str, Any]],
        system_prompt: str = SYSTEM_PROMPT,
        user_content_override: Optional[str] = None,
    ) -> Optional[Tuple[dict, str]]:
        now = time.monotonic()
        if now < self._gemini_cooldown_until:
            remaining = max(1, int(round(self._gemini_cooldown_until - now)))
            logger.info("Skipping Gemini during capacity cooldown (%ss remaining)", remaining)
            return None

        client = self._get_gemini_client()
        if not client:
            return None

        user_content = self._build_llm_user_content(query, candidates_summary, library_items, user_content_override)

        models_to_try = list(dict.fromkeys([settings.GEMINI_MODEL or self.gemini_model, "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]))
        for model in models_to_try:
            try:
                response = await asyncio.wait_for(
                    client.aio.models.generate_content(
                        model=model,
                        contents=user_content,
                        config=types.GenerateContentConfig(
                            system_instruction=system_prompt,
                            response_mime_type="application/json",
                            temperature=0.2,
                        ),
                    ),
                    timeout=8.0,
                )
                if response.text:
                    return json.loads(response.text), f"Google Gemini ({model})"
            except genai_errors.APIError as e:
                if self._record_gemini_failure(model, e, status_code=getattr(e, "code", None)):
                    break
            except asyncio.TimeoutError as e:
                self._record_gemini_failure(model, e)
                break
            except Exception as e:
                if self._record_gemini_failure(model, e):
                    break
        return None

    async def _call_github_models(
        self,
        query: str,
        candidates_summary: List[Dict[str, Any]],
        library_items: List[Dict[str, Any]],
        system_prompt: str = SYSTEM_PROMPT,
        user_content_override: Optional[str] = None,
    ) -> Optional[Tuple[dict, str]]:
        token = settings.GITHUB_TOKEN
        if not token:
            return None

        user_content = self._build_llm_user_content(query, candidates_summary, library_items, user_content_override)

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": settings.GITHUB_MODEL or self.github_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }

        try:
            client = get_search_client()
            res = await client.post(
                settings.GITHUB_MODELS_URL,
                headers=headers,
                json=payload,
                timeout=10.0,
            )
            if res.status_code == 200:
                data = res.json()
                content = data["choices"][0]["message"]["content"]
                return json.loads(content), f"GitHub Models ({settings.GITHUB_MODEL or self.github_model})"
        except Exception as e:
            logger.warning("GitHub Models error: %s", e)
        return None

    async def _call_azure_openai(
        self,
        query: str,
        candidates_summary: List[Dict[str, Any]],
        library_items: List[Dict[str, Any]],
        system_prompt: str = SYSTEM_PROMPT,
        user_content_override: Optional[str] = None,
    ) -> Optional[Tuple[dict, str]]:
        key = settings.AZURE_OPENAI_API_KEY
        if not key:
            return None

        deployment = settings.AZURE_OPENAI_DEPLOYMENT or "gpt-5-mini"
        endpoint = settings.AZURE_OPENAI_ENDPOINT or ""

        match = re.search(r"https://([^.]+)\.(?:services\.ai|openai)\.azure\.com", endpoint)
        if match:
            res_name = match.group(1)
            url = f"https://{res_name}.openai.azure.com/openai/deployments/{deployment}/chat/completions?api-version=2024-06-01"
        elif "deployments" in endpoint:
            url = endpoint
        else:
            url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version=2024-06-01"

        user_content = self._build_llm_user_content(query, candidates_summary, library_items, user_content_override)

        headers = {
            "api-key": key,
            "Content-Type": "application/json",
        }
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": 500,
        }

        try:
            client = get_search_client()
            res = await client.post(url, headers=headers, json=payload, timeout=8.0)
            if res.status_code == 200:
                data = res.json()
                content = data["choices"][0]["message"]["content"]
                return json.loads(content), f"Azure OpenAI ({deployment})"
            logger.warning("Azure OpenAI error: %s - %s", res.status_code, res.text[:150])
        except Exception as e:
            logger.warning("Azure OpenAI exception: %s", e)
        return None

    async def _dispatch_llm_providers(
        self,
        query: str,
        candidates_summary: List[Dict[str, Any]],
        library_items: List[Dict[str, Any]],
        system_prompt: str = SYSTEM_PROMPT,
        user_content_override: Optional[str] = None,
    ) -> Optional[Tuple[dict, str]]:
        provider = (settings.AI_PROVIDER or "auto").lower()
        if provider == "none":
            return None

        if provider == "azure":
            order = [
                (bool(settings.AZURE_OPENAI_API_KEY), self._call_azure_openai),
                (bool(settings.GEMINI_API_KEY), self._call_gemini),
                (bool(settings.GITHUB_TOKEN), self._call_github_models),
            ]
        elif provider == "gemini":
            order = [
                (bool(settings.GEMINI_API_KEY), self._call_gemini),
                (bool(settings.AZURE_OPENAI_API_KEY), self._call_azure_openai),
                (bool(settings.GITHUB_TOKEN), self._call_github_models),
            ]
        elif provider == "github":
            order = [
                (bool(settings.GITHUB_TOKEN), self._call_github_models),
                (bool(settings.AZURE_OPENAI_API_KEY), self._call_azure_openai),
                (bool(settings.GEMINI_API_KEY), self._call_gemini),
            ]
        else:  # "auto": gemini-3.5-flash-lite -> gpt-5-mini -> github models
            order = [
                (bool(settings.GEMINI_API_KEY), self._call_gemini),
                (bool(settings.AZURE_OPENAI_API_KEY), self._call_azure_openai),
                (bool(settings.GITHUB_TOKEN), self._call_github_models),
            ]

        for enabled, fn in order:
            if not enabled:
                continue
            try:
                res = await fn(
                    query,
                    candidates_summary,
                    library_items,
                    system_prompt=system_prompt,
                    user_content_override=user_content_override,
                )
                if res:
                    return res
            except Exception as e:
                logger.warning("LLM provider %s failed unexpectedly: %s", fn.__name__, e)
                sentry_sdk.add_breadcrumb(
                    category="ai.curator",
                    message=f"Provider {fn.__name__} failed unexpectedly",
                    level="warning",
                    data={"error": str(e)[:200]},
                )
        return None

    async def curate_top_candidates(
        self,
        query: str,
        releases: List[Dict[str, Any]],
        library_items: Optional[List[Dict[str, Any]]] = None,
        category: str = "All",
    ) -> Tuple[Optional[SmartRankingSummary], Dict[str, str], bool]:
        """Run or fetch cached 30m LLM curation over compressed top 8 releases."""
        if not releases:
            return None, {}, False

        # Strict privacy: never send Adult Vault queries, indexers, or Whisparr items to external AI providers
        cat_lower = (category or "").strip().lower()
        if "adult" in cat_lower or "xxx" in cat_lower or matches_adult_marker(query or "", include_adult_word=False):
            _, summary = self.enrich_and_score_heuristically(query, releases, library_items or [])
            return summary, {}, False

        safe_releases = [r for r in releases if not is_adult_item(r)]
        safe_library = [it for it in (library_items or []) if not is_adult_item(it)]
        if not safe_releases:
            _, summary = self.enrich_and_score_heuristically(query, releases, library_items or [])
            return summary, {}, False

        top_originals, compressed_candidates, top_hash = self.compress_candidates_for_llm(safe_releases, top_n=15)
        cache_key = f"{query.strip().lower()}::{top_hash}"

        cached = curator_cache.get(cache_key)
        if cached:
            return cached["summary"], cached["ratings"], True

        try:
            ai_result = await self._dispatch_llm_providers(
                query=query,
                candidates_summary=compressed_candidates,
                library_items=safe_library,
            )
            if not ai_result:
                _, heuristic_summary = self.enrich_and_score_heuristically(query, releases, library_items or [])
                return heuristic_summary, {}, False

            ai_data, model_name = ai_result
            if not isinstance(ai_data, dict):
                raise ValueError("LLM curator returned non-object JSON payload")

            title_to_item = {str(r.get("title", "")).strip(): r for r in top_originals}
            norm_title_to_item = {str(r.get("title", "")).strip().lower(): r for r in top_originals}
            guid_to_item = {str(r.get("guid", "")): r for r in top_originals}

            rec_title = ai_data.get("recommended_title")
            rec_guid = ai_data.get("recommended_guid")
            matched_rec = None
            if rec_guid and str(rec_guid) in guid_to_item:
                matched_rec = guid_to_item[str(rec_guid)]
            elif rec_title:
                matched_rec = title_to_item.get(str(rec_title).strip()) or norm_title_to_item.get(
                    str(rec_title).strip().lower()
                )
            if not matched_rec and top_originals:
                matched_rec = top_originals[0]

            ratings_map: Dict[str, str] = {}
            for entry in ai_data.get("ratings", []):
                if not isinstance(entry, dict):
                    continue
                note = entry.get("note")
                if not note:
                    continue
                e_guid = entry.get("guid")
                e_title = entry.get("title")
                target = None
                if e_guid and str(e_guid) in guid_to_item:
                    target = guid_to_item[str(e_guid)]
                elif e_title:
                    target = title_to_item.get(str(e_title).strip()) or norm_title_to_item.get(
                        str(e_title).strip().lower()
                    )
                if target and target.get("guid"):
                    ratings_map[str(target["guid"])] = str(note)

            summary = SmartRankingSummary(
                ai_active=True,
                query_intent=ai_data.get("query_intent", "Natural language media query"),
                recommended_guid=matched_rec.get("guid") if matched_rec else rec_guid,
                recommended_title=matched_rec.get("title") if matched_rec else rec_title,
                recommendation_reason=ai_data.get("recommendation_reason"),
                model_used=model_name,
            )

            curator_cache.set(cache_key, {"summary": summary, "ratings": ratings_map})
            return summary, ratings_map, False
        except Exception as exc:
            logger.warning("Curator LLM chain failed (%s): %s; falling back to Heuristic Rules Engine", type(exc).__name__, exc)
            sentry_sdk.add_breadcrumb(
                category="ai.curator",
                message="Curator fell back to heuristic rules engine",
                level="warning",
                data={"error_type": type(exc).__name__, "error": str(exc)[:200]},
            )
            _, heuristic_summary = self.enrich_and_score_heuristically(query, releases, library_items or [])
            return heuristic_summary, {}, False

    async def analyze_and_rank(
        self,
        query: str,
        releases: List[Dict[str, Any]],
        library_items: List[Dict[str, Any]],
        category: str = "All",
    ) -> Tuple[List[Dict[str, Any]], Optional[SmartRankingSummary]]:
        """Full heuristic + optional LLM ranking helper for callers opting into blocking curation."""
        enriched, heuristic_summary = self.enrich_and_score_heuristically(query, releases, library_items)
        if not enriched:
            return [], None

        summary, ratings_map, _ = await self.curate_top_candidates(
            query=query,
            releases=enriched,
            library_items=library_items,
            category=category,
        )
        if summary and summary.ai_active:
            for r in enriched:
                guid = r.get("guid")
                if guid == summary.recommended_guid:
                    r["ai_pick"] = True
                    r["ai_analysis"] = summary.recommendation_reason
                    r["quality_score"] = max(r.get("quality_score", 50), 98)
                else:
                    r["ai_pick"] = False
                if guid in ratings_map and not r.get("ai_analysis"):
                    r["ai_analysis"] = ratings_map[guid]
            return enriched, summary

        return enriched, heuristic_summary

    def _heuristic_maintenance_triage(self, report: Dict[str, Any]) -> Dict[str, Any]:
        actions: List[str] = []
        stalled = report.get("stalled_torrents") or []
        arr_warns = report.get("arr_warnings") or []
        idx_issues = report.get("indexer_issues") or []
        dups = report.get("duplicates") or []
        reclaim_gb = report.get("reclaimable_gb") or 0.0
        locked_gb = report.get("seeding_locked_gb") or 0.0

        if arr_warns:
            actions.append(f"Use Blacklist & Re-Search on {len(arr_warns)} flagged *arr queue item(s) to swap stalled/mismatched releases.")
        if stalled:
            actions.append(f"Inspect or replace {len(stalled)} qBittorrent download(s) stalled >6h with 0 seeds.")
        for idx in idx_issues[:3]:
            kind = idx.get("kind")
            name = idx.get("indexer")
            if kind == "auth_expired":
                actions.append(f"Refresh session cookies / credentials in Prowlarr for {name} (HTTP 401/403).")
            elif kind == "rate_limited":
                actions.append(f"Wait out cooldown or reduce query rate on {name} (HTTP 429).")
            else:
                actions.append(f"Check connectivity/proxy tunnel for {name}: {idx.get('message', '')[:80]}.")
        if dups or reclaim_gb > 0:
            actions.append(f"Safe duplicate cleanup can reclaim {reclaim_gb:.2f} GB while leaving {locked_gb:.2f} GB actively seeding untouched.")
        if not actions:
            actions.append("All seedbox queues, Prowlarr indexers, and library storage look healthy.")

        summary = (
            f"Detected {report.get('issue_count', 0)} active issue(s): "
            f"{len(stalled)} stalled torrents, {len(arr_warns)} *arr queue alerts, "
            f"{len(idx_issues)} indexer warning(s), {len(dups)} duplicate group(s)."
        )
        return {
            "ai_active": False,
            "model_used": "Heuristic Diagnostic Engine",
            "summary": summary,
            "recommended_actions": actions,
        }

    async def diagnose_maintenance_issues(self, report: Dict[str, Any]) -> Dict[str, Any]:
        """On-demand LLM triage (Gemini -> Azure -> GitHub -> Heuristic) over maintenance snapshot."""
        heuristic = self._heuristic_maintenance_triage(report)
        if not report.get("issue_count"):
            return heuristic

        # Strictly scrub Adult / Whisparr / Vault indexer references before any external LLM prompt leaves the server
        scrubbed_stalled = []
        for t in (report.get("stalled_torrents") or [])[:8]:
            adult_t = is_adult_item(t)
            scrubbed_stalled.append({
                "name": "[Vault Item]" if adult_t else _scrub_adult_text(str(t.get("name") or "")),
                "state": t.get("state"),
                "hours": t.get("stalled_hours"),
            })

        scrubbed_warnings = []
        for w in (report.get("arr_warnings") or [])[:8]:
            adult_w = str(w.get("service") or "").strip().lower() == "whisparr"
            scrubbed_warnings.append({
                "service": "[Vault]" if adult_w else w.get("service"),
                "title": "[Vault Item]" if adult_w else _scrub_adult_text(str(w.get("title") or "")),
                "messages": ["[Vault Item]"] if adult_w else [_scrub_adult_text(str(m)) for m in (w.get("messages") or [])[:2]],
            })

        scrubbed_indexers = []
        for i in (report.get("indexer_issues") or [])[:8]:
            idx_name = str(i.get("indexer") or "")
            idx_msg = str(i.get("message") or "")[:120]
            if matches_adult_marker(idx_name) or matches_adult_marker(idx_msg):
                scrubbed_indexers.append({"indexer": "[Vault Item]", "kind": i.get("kind"), "message": "[Vault Item]"})
            else:
                scrubbed_indexers.append({"indexer": idx_name, "kind": i.get("kind"), "message": idx_msg})

        scrubbed = {
            "disk_free": report.get("disk_free_formatted"),
            "low_disk_space": report.get("low_disk_space"),
            "reclaimable_gb": report.get("reclaimable_gb"),
            "seeding_locked_gb": report.get("seeding_locked_gb"),
            "stalled_torrents": scrubbed_stalled,
            "arr_warnings": scrubbed_warnings,
            "indexer_issues": scrubbed_indexers,
            "duplicate_groups": len(report.get("duplicates") or []),
            "heavy_files_count": len(report.get("heavy_files") or []),
        }
        diag_system = (
            "You are a concise media-server DevOps & seedbox diagnostician. "
            "Respond ONLY with JSON matching {\"summary\": str, \"recommended_actions\": [str, ...]}. "
            "Never advise deleting files marked seeding/hardlinked."
        )
        user_prompt = f"Diagnose this seedbox/arr watchdog snapshot and prioritize concrete fixes:\n{json.dumps(scrubbed, separators=(',', ':'))}"
        try:
            ai_res = await self._dispatch_llm_providers(
                query="maintenance-triage",
                candidates_summary=[],
                library_items=[],
                system_prompt=diag_system,
                user_content_override=user_prompt,
            )
            if ai_res and isinstance(ai_res[0], dict):
                payload, model_name = ai_res
                return {
                    "ai_active": True,
                    "model_used": model_name,
                    "summary": str(payload.get("summary") or heuristic["summary"]),
                    "recommended_actions": [str(a) for a in (payload.get("recommended_actions") or heuristic["recommended_actions"])][:6],
                }
        except Exception as exc:
            logger.warning("AI maintenance triage fell back to heuristics: %s", exc)
        return heuristic


ai_assistant = MultiProviderReleaseAssistant()
