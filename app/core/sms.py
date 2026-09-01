"""SMS dispatch for the Alert & Notification Module, via Twilio's REST
API. Uses Twilio because it's the most widely available SMS provider
with a free trial, and its API is a single authenticated POST request
- so this uses Python's built-in urllib instead of adding the twilio
SDK as a new dependency.

Needs a Twilio account (https://www.twilio.com/try-twilio): the
Account SID, Auth Token, and a Twilio phone number to send from, all
set in .env (see TWILIO_* below). Without those set, this degrades
the same way app/core/email.py does when SMTP isn't configured - it
logs "not configured" per recipient instead of raising.

Phase 10 - transient-failure retry (see docs/notification-delivery.md)
-----------------------------------------------------------------
Mirrors the retry classifier added to app/core/email.py: a network-
level failure reaching Twilio at all (`urllib.error.URLError` -
connection refused, DNS failure, timeout) or a 5xx/429 ("rate
limited"/"try again") HTTP response is TRANSIENT and gets retried
with backoff. A 4xx response other than 429 (bad auth, invalid
number, malformed request) is PERMANENT - retrying it would just
waste the backoff window on something that can never succeed. Same
NOTIFICATION_SEND_MAX_ATTEMPTS/NOTIFICATION_SEND_RETRY_BACKOFF_*
settings as email, and the same one-outcome-per-recipient contract.
"""
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
    """True for a network-level failure to reach Twilio at all, or an
    HTTP 429 ("too many requests")/5xx ("their problem, try again")
    response. False for any other HTTPError (4xx - bad auth, invalid
    number, malformed request - permanent, retrying can't help)."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        # Covers connection-refused/DNS/timeout - HTTPError is also a
        # URLError subclass, so this branch only ever sees the
        # non-HTTP cases (no response was received at all).
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
    """Send the alert SMS to every recipient phone number, retrying a
    TRANSIENT failure (see `_is_transient_sms_error`) up to
    `settings.NOTIFICATION_SEND_MAX_ATTEMPTS` times with backoff
    before giving up on that recipient. A permanent failure (bad
    auth, invalid number) fails on the first attempt, no retry.

    Set `resolved=True` to send the short "this has been resolved"
    version instead of the original alert text - used by
    dispatch_alert_resolution_notifications() in alert_service.py.

    Returns {phone: "sent"} or {phone: "failed: <reason>"} per
    recipient - exactly one outcome per recipient no matter how many
    attempts it took - mirrors app/core/email.py's send_alert_emails()
    return shape so alert_service.py can log both channels the same
    way. Never raises.
    """
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
                    # urlopen raises HTTPError itself for any non-2xx
                    # status, so reaching here means success.
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
