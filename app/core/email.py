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

    recipients = getattr(exc, "recipients", None)
    if isinstance(recipients, dict) and recipients:
        codes = [c for c, _ in recipients.values() if isinstance(c, int)]
        if codes:
            return min(codes)
    return None

def _is_transient_smtp_error(exc: Exception) -> bool:
    if isinstance(exc, smtplib.SMTPServerDisconnected):

        return True
    if isinstance(exc, smtplib.SMTPException):
        code = _smtp_code_of(exc)
        if code is not None:
            return 400 <= code < 500

        return False

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
