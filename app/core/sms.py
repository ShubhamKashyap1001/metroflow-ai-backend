import base64
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from app.core.config import settings

logger = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01/Accounts"

def _build_alert_sms(
    station_name: str,
    alert_type: str,
    message: str,
    available_until: str | None = None,
    resolved: bool = False,
) -> str:
                                                                    
    if resolved:
        text = f"[MetroFlow] RESOLVED - {alert_type.upper()} at {station_name}: {message}"
    else:
        text = f"[MetroFlow] {alert_type.upper()} at {station_name}: {message}"
        if available_until:
            text += f" Expected back by {available_until}."
    return text[:300]

def _is_transient_sms_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, urllib.error.URLError):

        return True
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))

def send_alert_sms(
    recipients: list[str],
    station_name: str,
    alert_type: str,
    message: str,
    available_until: str | None = None,
    resolved: bool = False,
) -> dict[str, str]:
    results: dict[str, str] = {}

    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN or not settings.TWILIO_FROM_NUMBER:
        for recipient in recipients:
            results[recipient] = "failed: Twilio not configured"
        return results

    body_text = _build_alert_sms(station_name, alert_type, message, available_until, resolved)
    url = f"{TWILIO_API_BASE}/{settings.TWILIO_ACCOUNT_SID}/Messages.json"

    auth = base64.b64encode(
        f"{settings.TWILIO_ACCOUNT_SID}:{settings.TWILIO_AUTH_TOKEN}".encode()
    ).decode()

    max_attempts = max(settings.NOTIFICATION_SEND_MAX_ATTEMPTS, 1)
    backoff_floor = settings.NOTIFICATION_SEND_RETRY_BACKOFF_SECONDS
    backoff_cap = settings.NOTIFICATION_SEND_RETRY_BACKOFF_CAP_SECONDS

    for recipient in recipients:
        payload = urllib.parse.urlencode(
            {
                "From": settings.TWILIO_FROM_NUMBER,
                "To": recipient,
                "Body": body_text,
            }
        ).encode()

        last_exc: Exception | None = None
        backoff = backoff_floor
        for attempt in range(1, max_attempts + 1):
            try:
                req = urllib.request.Request(url, data=payload, method="POST")
                req.add_header("Authorization", f"Basic {auth}")
                req.add_header("Content-Type", "application/x-www-form-urlencoded")

                with urllib.request.urlopen(req, timeout=15) as resp:

                    results[recipient] = "sent"
                    last_exc = None
                break
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode()[:200]
                except Exception:
                    detail = str(exc)
                last_exc = urllib.error.HTTPError(exc.url, exc.code, detail, exc.headers, exc.fp)
                if not _is_transient_sms_error(exc) or attempt == max_attempts:
                    break
                logger.warning(
                    "[sms] transient send failure to %s (attempt %d/%d): HTTP %s - "
                    "retrying in %.1fs.",
                    recipient, attempt, max_attempts, exc.code, backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, backoff_cap)
            except Exception as exc:
                last_exc = exc
                if not _is_transient_sms_error(exc) or attempt == max_attempts:
                    break
                logger.warning(
                    "[sms] transient send failure to %s (attempt %d/%d): %s - "
                    "retrying in %.1fs.",
                    recipient, attempt, max_attempts, exc, backoff,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, backoff_cap)

        if last_exc is not None:
            results[recipient] = f"failed: {last_exc}"

    return results
