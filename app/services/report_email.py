from datetime import datetime, timezone
from email.message import EmailMessage
import logging
import smtplib
import ssl
import time

from app.core.config import settings
from app.core.email import _is_transient_smtp_error

logger = logging.getLogger(__name__)

def send_report_email(recipient: str, csv_content: str, subject: str, summary: str) -> str:
    if not settings.SMTP_HOST or not settings.SMTP_USERNAME or not settings.SMTP_FROM_EMAIL:
        return "failed: SMTP is not configured"

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"{settings.SMTP_FROM_NAME} <{settings.SMTP_FROM_EMAIL}>"
    message["To"] = recipient
    message.set_content(
        f"Your scheduled MetroFlow report is attached as a CSV file.\n\n{summary}\n\n"
        "This message was sent automatically by MetroFlow."
    )
    message.add_attachment(
        csv_content.encode("utf-8-sig"),
        maintype="text",
        subtype="csv",
        filename=f"metroflow-traffic-report-{datetime.now(timezone.utc):%Y%m%d}.csv",
    )

    attempts = max(settings.NOTIFICATION_SEND_MAX_ATTEMPTS, 1)
    for attempt in range(attempts):
        try:
            context = ssl.create_default_context()
            if settings.SMTP_USE_TLS:
                with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15) as server:
                    server.starttls(context=context)
                    server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD or "")
                    server.send_message(message)
            else:
                with smtplib.SMTP_SSL(
                    settings.SMTP_HOST,
                    settings.SMTP_PORT,
                    context=context,
                    timeout=15,
                ) as server:
                    server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD or "")
                    server.send_message(message)
            return "sent"
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            if not _is_transient_smtp_error(exc) or attempt + 1 >= attempts:
                logger.warning("Scheduled report email failed for %s: %s", recipient, exc)
                return f"failed: {str(exc)[:450]}"
            time.sleep(min(settings.NOTIFICATION_SEND_RETRY_BACKOFF_SECONDS * (2**attempt), settings.NOTIFICATION_SEND_RETRY_BACKOFF_CAP_SECONDS))
    return "failed: SMTP delivery did not complete"
