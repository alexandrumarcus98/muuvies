"""Optional Sentry error and latency observability with strict privacy scrubbing."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from backend.app.services.adult_preview import get_preview_privacy_hosts

logger = logging.getLogger("muuvies.observability")

_SENSITIVE_HEADERS = frozenset({
    "cookie",
    "set-cookie",
    "authorization",
    "x-api-key",
    "x-vault-pin",
    "x-csrftoken",
})

_SENSITIVE_FIELD_FRAGMENTS = (
    "api_key",
    "apikey",
    "password",
    "secret",
    "token",
    "vault_pin",
    "pin",
    "cookie",
    "fernet",
    "sentry_dsn",
    "dsn",
)


def _is_vault_or_adult_payload(raw_value: Any) -> bool:
    """Detect whether text/mapping references Adult Vault routes, adult categories, or preview hosts."""
    if raw_value is None:
        return False
    if isinstance(raw_value, str):
        lowered = raw_value.lower()
        if "/api/vault" in lowered:
            return True
        if '"category": "adult"' in lowered or '"category":"adult"' in lowered:
            return True
        if "category=adult" in lowered:
            return True
        if any(host in lowered for host in get_preview_privacy_hosts()):
            return True
        return False
    if isinstance(raw_value, dict):
        cat = str(raw_value.get("category", "")).strip().lower()
        if cat == "adult":
            return True
        return any(_is_vault_or_adult_payload(v) for v in raw_value.values())
    if isinstance(raw_value, (list, tuple)):
        return any(_is_vault_or_adult_payload(item) for item in raw_value)
    return False


def _scrub_mapping(obj: Any) -> Any:
    """Recursively redact secrets and cookies inside Sentry event dictionaries."""
    if isinstance(obj, dict):
        cleaned: Dict[str, Any] = {}
        for k, v in obj.items():
            key_lower = str(k).lower()
            if key_lower in _SENSITIVE_HEADERS or any(frag in key_lower for frag in _SENSITIVE_FIELD_FRAGMENTS):
                cleaned[k] = "[Filtered]"
            else:
                cleaned[k] = _scrub_mapping(v)
        return cleaned
    if isinstance(obj, list):
        return [_scrub_mapping(item) for item in obj]
    return obj


def scrub_sentry_event(event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Strip PII, session cookies, decrypted secrets, and drop Adult Vault events."""
    req = event.get("request")
    if isinstance(req, dict):
        url = str(req.get("url", ""))
        query_string = str(req.get("query_string", ""))
        data = req.get("data")
        if (
            _is_vault_or_adult_payload(url)
            or _is_vault_or_adult_payload(query_string)
            or _is_vault_or_adult_payload(data)
        ):
            return None

        req.pop("cookies", None)
        headers = req.get("headers")
        if isinstance(headers, dict):
            req["headers"] = {
                k: ("[Filtered]" if str(k).lower() in _SENSITIVE_HEADERS else v)
                for k, v in headers.items()
            }
        if data is not None:
            if isinstance(data, str):
                try:
                    parsed = json.loads(data)
                    if _is_vault_or_adult_payload(parsed):
                        return None
                    req["data"] = json.dumps(_scrub_mapping(parsed))
                except Exception:
                    pass
            else:
                req["data"] = _scrub_mapping(data)

    transaction = str(event.get("transaction", ""))
    if _is_vault_or_adult_payload(transaction):
        return None

    # Scrub breadcrumbs (drop vault/adult/preview-host breadcrumbs, redact headers)
    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict) and isinstance(breadcrumbs.get("values"), list):
        filtered_crumbs = []
        for crumb in breadcrumbs["values"]:
            if not isinstance(crumb, dict):
                continue
            crumb_data = crumb.get("data")
            if _is_vault_or_adult_payload(crumb):
                continue
            if isinstance(crumb_data, dict):
                crumb = {**crumb, "data": _scrub_mapping(crumb_data)}
            filtered_crumbs.append(crumb)
        breadcrumbs["values"] = filtered_crumbs

    for section in ("extra", "contexts", "tags"):
        if isinstance(event.get(section), dict):
            event[section] = _scrub_mapping(event[section])

    return event


def scrub_sentry_transaction(
    event: Dict[str, Any], hint: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Apply identical Vault & PII scrubbing to Sentry performance transactions."""
    return scrub_sentry_event(event, hint)


def init_sentry(dsn: Optional[str]) -> bool:
    """Initialize Sentry FastAPI SDK if a non-empty DSN is provided; otherwise no-op."""
    cleaned_dsn = (dsn or "").strip()
    if not cleaned_dsn:
        return False

    sentry_sdk.init(
        dsn=cleaned_dsn,
        traces_sample_rate=0.2,
        send_default_pii=False,
        before_send=scrub_sentry_event,
        before_send_transaction=scrub_sentry_transaction,
        integrations=[
            StarletteIntegration(),
            FastApiIntegration(),
        ],
    )
    logger.info("Sentry observability initialized (send_default_pii=False, traces_sample_rate=0.2)")
    return True

