import logging
from urllib.parse import urlparse

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

def _dispatch(url: str | None, token: str | None, payload: dict, channel: str) -> None:
    if not url or not token:
        return

    parsed = urlparse(url)
    local_http = settings.DEBUG and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not local_http:
        logger.error("%s webhook must use HTTPS", channel)
        return

    try:
        response = httpx.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(8.0, connect=3.0),
            follow_redirects=False,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        logger.error("%s webhook delivery failed: %s", channel, type(exc).__name__)

def dispatch_emergency_alert(payload: dict) -> None:
    _dispatch(settings.PUBLIC_ADDRESS_WEBHOOK_URL, settings.PUBLIC_ADDRESS_WEBHOOK_TOKEN, payload, "Public address")

def dispatch_mobile_push_alert(payload: dict) -> None:
    _dispatch(settings.MOBILE_PUSH_WEBHOOK_URL, settings.MOBILE_PUSH_WEBHOOK_TOKEN, payload, "Mobile push")
