"""SMTP email dispatch for the Alert & Notification Module.

Uses a generic SMTP connection - works with Gmail (App Password),
SendGrid SMTP relay, Mailgun, AWS SES SMTP, etc. No third-party SDK
needed since smtplib/email are part of the Python standard library,
so nothing new to add to requirements.txt.

Phase 10 - transient-failure retry (see docs/notification-delivery.md)
-----------------------------------------------------------------
Before this phase, a single bad moment on the SMTP side - the
provider dropping the connection, a connect timeout, a "4xx, try
again later" response while it's temporarily overloaded - was
indistinguishable from a permanent failure (a bad address, an auth
error): one attempt, then `results[recipient] = "failed: ..."`
forever. That's real notification loss for exactly the case that's
supposed to be recoverable: the outage was temporary, but the
notification wasn't retried, so it never arrived.

`_is_transient_smtp_error()` below draws the line: connection-level
problems and 4xx SMTP codes are treated as "worth retrying" (the
provider is saying/implying "try again"), while a 5xx code, a
rejected recipient/sender, or an auth failure are treated as
permanent - retrying those would just burn the whole backoff window
before failing anyway. Only the transient bucket gets retried, with a
small bounded number of attempts and exponential backoff
(NOTIFICATION_SEND_MAX_ATTEMPTS / NOTIFICATION_SEND_RETRY_BACKOFF_*
in app/core/config.py) - each retry reconnects from scratch, since a
dropped/broken connection can't just resume.

This never sends the same recipient's email twice for one call: each
recipient gets AT MOST ONE outcome ("sent" or "failed: ..."), retried
in place before moving on - `send_alert_emails` still returns exactly
one result per input recipient.
"""
import logging
import smtplib
import ssl
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)

def _build_alert_email(
    station_name: str,
    alert_type: str,
    message: str,
    created_at: str,
    available_until: str | None = None,
    resolved: bool = False,
) -> MIMEMultipart:
    if resolved:
        subject = f"[MetroFlow Alert] RESOLVED - {alert_type.upper()} at {station_name}"
        banner_color = "#15803d"
        banner_text = "Issue Resolved"
        lead = (
            "The issue below has been marked resolved. Normal service "
            "has now resumed."
        )
    else:
        subject = f"[MetroFlow Alert] {alert_type.upper()} - {station_name}"
        banner_color = "#0f172a"
        banner_text = "MetroFlow Alert"
        lead = None

    available_row = ""
    if available_until and not resolved:
        available_row = (
            f'<p style="margin:0 0 8px 0;">'
            f"<strong>Service expected back by:</strong> {available_until}</p>"
        )

    lead_html = (
        f'<p style="margin:0 0 16px 0; color:#15803d; font-weight:600;">{lead}</p>'
        if lead
        else ""
    )

    html = f"""
    <div style="font-family: Arial, sans-serif; max-width: 560px; margin: auto;">
      <div style="background:{banner_color}; padding:20px; border-radius:12px 12px 0 0;">
        <h2 style="color:#ffffff; margin:0;">{banner_text}</h2>
      </div>
      <div style="border:1px solid #e2e8f0; border-top:none; padding:24px; border-radius:0 0 12px 12px;">
        {lead_html}
        <p style="margin:0 0 8px 0;"><strong>Station:</strong> {station_name}</p>
        <p style="margin:0 0 8px 0;"><strong>Type:</strong> {alert_type.title()}</p>
        <p style="margin:0 0 16px 0;"><strong>Time:</strong> {created_at}</p>
        {available_row}
        <p style="font-size:15px; line-height:1.5; color:#334155;">{message}</p>
      </div>
      <p style="font-size:12px; color:#94a3b8; margin-top:16px;">
        This is an automated notification from MetroFlow. Please do not reply to this email.
      </p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    msg.attach(MIMEText(html, "html"))
    return msg

def _smtp_code_of(exc: Exception) -> int | None:
    code = getattr(exc, "smtp_code", None)
    if isinstance(code, int):
        return code
    # SMTPRecipientsRefused carries one code per recipient instead of
    # a single smtp_code - look at those instead.
    recipients = getattr(exc, "recipients", None)
    if isinstance(recipients, dict) and recipients:
        codes = [c for c, _ in recipients.values() if isinstance(c, int)]
        if codes:
            return min(codes)
    return None

def _is_transient_smtp_error(exc: Exception) -> bool:
    """True if `exc` represents a temporary condition worth retrying
    (dropped connection, connection-refused/timed-out before the SMTP
    session even started, or an explicit 4xx "try again later" SMTP
    response) rather than a permanent one (bad address, rejected
    sender, 5xx, auth failure).

    NOTE: `smtplib.SMTPException` itself subclasses `OSError`, so a
    plain `isinstance(exc, OSError)` check would also match every
    protocol-level SMTP error (SMTPRecipientsRefused, etc.) and treat
    permanent 5xx rejections as retryable. SMTP-protocol exceptions
    are therefore classified by their status code FIRST; only a
    non-SMTP exception falls through to the raw
    connection/timeout/OSError check below.
    """
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        # The connection dropped mid-session with no status code at
        # all - always worth a fresh retry.
        return True
    if isinstance(exc, smtplib.SMTPException):
        code = _smtp_code_of(exc)
        if code is not None:
            return 400 <= code < 500
        # No status code to go on (e.g. some SMTPAuthenticationError
        # cases) - treat as permanent rather than retry blind.
        return False
    # Not an SMTP-protocol exception at all: a genuine transport-level
    # failure (connection refused, DNS failure, connect timeout).
    return isinstance(exc, (TimeoutError, ConnectionError, OSError))

def send_alert_emails(
    recipients: list[str],
    station_name: str,
    alert_type: str,
    message: str,
    created_at: str,
    available_until: str | None = None,
    resolved: bool = False,
) -> dict[str, str]:
    """Send the alert email to every recipient over a single SMTP
    connection, retrying each recipient (with a fresh reconnect) up to
    `settings.NOTIFICATION_SEND_MAX_ATTEMPTS` times if the failure
    looks TRANSIENT (see `_is_transient_smtp_error`) - a permanent
    failure (bad address, rejected sender, auth error) fails on the
    first attempt with no retry, since retrying it can't succeed.

    Set `resolved=True` to send the "this has been resolved" version
    of the email instead of the original alert email - used by
    dispatch_alert_resolution_notifications() in alert_service.py.

    Returns a dict of {email: "sent"} or {email: "failed: <reason>"}
    per recipient - exactly one outcome per recipient no matter how
    many attempts it took - this function never raises, so one bad
    address or a dropped connection mid-batch can't take down the
    whole request (it runs inside app/core/notification_executor.py's
    own thread pool anyway, but staying defensive here keeps the
    per-recipient NotificationLog accurate).
    """
    results: dict[str, str] = {}

    if not settings.SMTP_HOST or not settings.SMTP_USERNAME:
        for recipient in recipients:
            results[recipient] = "failed: SMTP not configured"
        return results

    email_msg = _build_alert_email(
        station_name, alert_type, message, created_at, available_until, resolved
    )

    def _connect() -> smtplib.SMTP:
        context = ssl.create_default_context()
        if settings.SMTP_USE_TLS:
            conn = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
            conn.starttls(context=context)
        else:
            conn = smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT, context=context, timeout=15
            )
        conn.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        return conn

    max_attempts = max(settings.NOTIFICATION_SEND_MAX_ATTEMPTS, 1)
    backoff_floor = settings.NOTIFICATION_SEND_RETRY_BACKOFF_SECONDS
    backoff_cap = settings.NOTIFICATION_SEND_RETRY_BACKOFF_CAP_SECONDS

    server: smtplib.SMTP | None = None
    try:
        try:
            server = _connect()
        except Exception as exc:
            # Couldn't even establish the initial connection - still
            # give it the same transient-retry treatment per recipient
            # below rather than failing the whole batch outright, in
            # case it recovers a moment later.
            server = None
            initial_connect_error: Exception | None = exc
        else:
            initial_connect_error = None

        for recipient in recipients:
            del email_msg["To"]
            email_msg["To"] = recipient

            last_exc: Exception | None = initial_connect_error
            backoff = backoff_floor
            for attempt in range(1, max_attempts + 1):
                try:
                    if server is None:
                        server = _connect()
                    server.sendmail(
                        settings.SMTP_FROM_EMAIL, recipient, email_msg.as_string()
                    )
                    results[recipient] = "sent"
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    try:
                        if server is not None:
                            server.quit()
                    except Exception:
                        pass
                    server = None

                    if not _is_transient_smtp_error(exc) or attempt == max_attempts:
                        break

                    logger.warning(
                        "[email] transient send failure to %s (attempt %d/%d): %s - "
                        "retrying in %.1fs.",
                        recipient, attempt, max_attempts, exc, backoff,
                    )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, backoff_cap)

            if last_exc is not None:
                results[recipient] = f"failed: {last_exc}"
    finally:
        try:
            if server is not None:
                server.quit()
        except Exception:
            pass

    return results
