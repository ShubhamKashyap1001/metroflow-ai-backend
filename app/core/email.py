import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

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
    connection.

    Set `resolved=True` to send the "this has been resolved" version
    of the email instead of the original alert email - used by
    dispatch_alert_resolution_notifications() in alert_service.py.

    Returns a dict of {email: "sent"} or {email: "failed: <reason>"}
    per recipient - this function never raises, so one bad address or
    a dropped connection mid-batch can't take down the whole request
    (it runs inside a FastAPI BackgroundTask anyway, but staying
    defensive here keeps the per-recipient NotificationLog accurate).
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

    server = None
    try:
        server = _connect()

        for recipient in recipients:
            try:
                                                                      
                del email_msg["To"]
                email_msg["To"] = recipient

                try:
                    server.sendmail(
                        settings.SMTP_FROM_EMAIL, recipient, email_msg.as_string()
                    )
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError):
                                                                    
                    try:
                        server.quit()
                    except Exception:
                        pass
                    server = _connect()
                    server.sendmail(
                        settings.SMTP_FROM_EMAIL, recipient, email_msg.as_string()
                    )

                results[recipient] = "sent"
            except Exception as exc:
                results[recipient] = f"failed: {exc}"

    except Exception as exc:
                                                                     
        for recipient in recipients:
            if recipient not in results:
                results[recipient] = f"failed: {exc}"
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass

    return results