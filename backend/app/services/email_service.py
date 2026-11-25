"""Transactional email delivery via Resend REST API using the shared HTTPX pool."""

from __future__ import annotations

import html
import logging

import httpx

from backend.app.config import settings
from backend.app.services.http_clients import get_search_client

logger = logging.getLogger("muuvies.email")

RESEND_EMAILS_URL = "https://api.resend.com/emails"


async def send_email(
    to_email: str,
    subject: str,
    html_body: str,
    text_body: str = "",
) -> bool:
    """Send a transactional email via Resend REST API, or log cleanly in local dev/tests."""
    api_key = (settings.RESEND_API_KEY or "").strip()
    if not api_key:
        logger.info("RESEND_API_KEY not configured; %s -> %s | %s", subject, to_email, text_body.strip())
        return True

    payload = {
        "from": settings.EMAIL_FROM,
        "to": [to_email],
        "subject": subject,
        "html": html_body,
    }
    if text_body:
        payload["text"] = text_body

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        client = get_search_client()
        res = await client.post(RESEND_EMAILS_URL, headers=headers, json=payload, timeout=10.0)
        if 200 <= res.status_code < 300:
            return True
        logger.warning("Resend email delivery failed (HTTP %s): %s", res.status_code, res.text[:200])
        return False
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("Resend HTTP error sending '%s' (%s): %s", subject, type(exc).__name__, exc)
        return False


async def send_verification_email(to_email: str, verify_url: str) -> bool:
    """Send an account verification link via Resend, or log cleanly in local dev/tests."""
    safe_url = html.escape(verify_url, quote=True)
    html_body = (
        "<div style=\"font-family:Inter,system-ui,sans-serif;max-width:480px;margin:0 auto;padding:24px;\">"
        "<h2 style=\"margin:0 0 12px;font-size:18px;color:#09090b;\">Verify your Muuvies account</h2>"
        "<p style=\"margin:0 0 20px;font-size:14px;line-height:1.5;color:#52525b;\">"
        "Click the button below to confirm your email address. This link expires in 24 hours."
        "</p>"
        f"<a href=\"{safe_url}\" style=\"display:inline-block;padding:10px 18px;background:#09090b;"
        "color:#fafafa;text-decoration:none;border-radius:8px;font-size:14px;font-weight:500;\">"
        "Verify Email Address</a>"
        "<p style=\"margin:20px 0 0;font-size:12px;color:#71717a;word-break:break-all;\">"
        f"Or copy this URL: {safe_url}</p></div>"
    )
    text_body = (
        "Verify your Muuvies account\n\n"
        f"Open this link within 24 hours to verify your email: {verify_url}\n"
    )
    return await send_email(to_email, "Verify your Muuvies account", html_body, text_body)


async def send_password_reset_email(to_email: str, reset_url: str) -> bool:
    """Send a single-use 30-minute password reset link via Resend."""
    safe_url = html.escape(reset_url, quote=True)
    html_body = (
        "<div style=\"font-family:Inter,system-ui,sans-serif;max-width:480px;margin:0 auto;padding:24px;\">"
        "<h2 style=\"margin:0 0 12px;font-size:18px;color:#09090b;\">Reset your Muuvies password</h2>"
        "<p style=\"margin:0 0 20px;font-size:14px;line-height:1.5;color:#52525b;\">"
        "Use the button below to set a new password. This link expires in 30 minutes and invalidates all active sessions."
        "</p>"
        f"<a href=\"{safe_url}\" style=\"display:inline-block;padding:10px 18px;background:#09090b;"
        "color:#fafafa;text-decoration:none;border-radius:8px;font-size:14px;font-weight:500;\">"
        "Reset Password</a>"
        "<p style=\"margin:20px 0 0;font-size:12px;color:#71717a;word-break:break-all;\">"
        f"Or copy this URL: {safe_url}</p></div>"
    )
    text_body = (
        "Reset your Muuvies password\n\n"
        f"Open this link within 30 minutes to set a new password: {reset_url}\n"
    )
    return await send_email(to_email, "Reset your Muuvies password", html_body, text_body)

