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
"""
import base64
import urllib.error
import urllib.parse
import urllib.request

from app.core.config import settings

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

def send_alert_sms(
    recipients: list[str],
    station_name: str,
    alert_type: str,
    message: str,
    available_until: str | None = None,
    resolved: bool = False,
) -> dict[str, str]:
    """Send the alert SMS to every recipient phone number.

    Set `resolved=True` to send the short "this has been resolved"
    version instead of the original alert text - used by
    dispatch_alert_resolution_notifications() in alert_service.py.

    Returns {phone: "sent"} or {phone: "failed: <reason>"} per
    recipient - mirrors app/core/email.py's send_alert_emails() return
    shape so alert_service.py can log both channels the same way.
    Never raises.
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

    for recipient in recipients:
        try:
            payload = urllib.parse.urlencode(
                {
                    "From": settings.TWILIO_FROM_NUMBER,
                    "To": recipient,
                    "Body": body_text,
                }
            ).encode()

            req = urllib.request.Request(url, data=payload, method="POST")
            req.add_header("Authorization", f"Basic {auth}")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

            with urllib.request.urlopen(req, timeout=15) as resp:
                if 200 <= resp.status < 300:
                    results[recipient] = "sent"
                else:
                    results[recipient] = f"failed: Twilio returned HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
                                                                     
            try:
                detail = exc.read().decode()[:200]
            except Exception:
                detail = str(exc)
            results[recipient] = f"failed: {detail}"
        except Exception as exc:
            results[recipient] = f"failed: {exc}"

    return results
