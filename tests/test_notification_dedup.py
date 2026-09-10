
import io
import smtplib
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.core.config import settings



from app.core import email as email_mod


def _fake_request(path="/api/v1/alerts/42/resolve", method="PATCH"):
    """A minimal but real starlette.requests.Request - needed because
    slowapi's @limiter.limit decorator does `isinstance(request,
    Request)` on whatever the endpoint was called with, so a
    duck-typed stand-in (like tests/test_metrics.py's _FakeRequest,
    which only needs to satisfy route_label()'s much narrower needs)
    isn't enough here. This carries just enough ASGI scope for
    slowapi's default key_func (get_ipaddr, reads request.client) and
    for it to set request.state.view_rate_limit.
    """
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": b"",
        "client": ("testclient", 12345),
        "server": ("testserver", 80),
        "scheme": "http",
    }
    return Request(scope)


def _fake_smtp_response(status=201):
    cm = MagicMock()
    cm.__enter__.return_value = MagicMock(status=status)
    cm.__exit__.return_value = False
    return cm


@pytest.fixture(autouse=True)
def _email_settings(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(settings, "SMTP_USERNAME", "user")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pass")
    monkeypatch.setattr(settings, "SMTP_FROM_EMAIL", "alerts@metroflow.app")
    monkeypatch.setattr(settings, "SMTP_FROM_NAME", "MetroFlow Alerts")
    monkeypatch.setattr(settings, "SMTP_USE_TLS", True)
    monkeypatch.setattr(settings, "NOTIFICATION_SEND_MAX_ATTEMPTS", 3)
    monkeypatch.setattr(settings, "NOTIFICATION_SEND_RETRY_BACKOFF_SECONDS", 0.01)
    monkeypatch.setattr(settings, "NOTIFICATION_SEND_RETRY_BACKOFF_CAP_SECONDS", 0.02)


def test_email_success_sends_on_first_attempt():
    """SUCCESS case: a healthy provider sends on the first try, no
    retry involved at all."""
    server = MagicMock()
    with patch.object(email_mod.smtplib, "SMTP", return_value=server), \
         patch.object(email_mod.smtplib, "SMTP_SSL", return_value=server), \
         patch.object(email_mod.ssl, "create_default_context"):
        results = email_mod.send_alert_emails(
            ["a@example.com"], "Central", "delay", "msg", "2026-01-01T00:00:00Z",
        )
    assert results == {"a@example.com": "sent"}
    assert server.sendmail.call_count == 1


def test_email_transient_provider_outage_recovers_via_retry():
    """RETRY case: the provider drops the connection once (a temporary
    outage) and the retry succeeds - the notification is NOT lost."""
    attempts = {"n": 0}

    def flaky_sendmail(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise smtplib.SMTPServerDisconnected("connection dropped")
        return None

    server = MagicMock()
    server.sendmail.side_effect = flaky_sendmail
    with patch.object(email_mod.smtplib, "SMTP", return_value=server), \
         patch.object(email_mod.smtplib, "SMTP_SSL", return_value=server), \
         patch.object(email_mod.ssl, "create_default_context"), \
         patch.object(email_mod.time, "sleep") as mock_sleep:
        results = email_mod.send_alert_emails(
            ["b@example.com"], "Central", "delay", "msg", "2026-01-01T00:00:00Z",
        )
    assert results == {"b@example.com": "sent"}
    assert attempts["n"] == 2, "expected exactly one retry, not more/less"
    mock_sleep.assert_called_once()


def test_email_permanent_failure_is_not_retried():
    """FAILURE case (permanent): a rejected recipient (550) is not a
    transient condition - it fails on the first attempt, with no
    wasted retries."""
    attempts = {"n": 0}

    def refused_sendmail(*args, **kwargs):
        attempts["n"] += 1
        raise smtplib.SMTPRecipientsRefused(
            {"c@example.com": (550, b"mailbox unavailable")}
        )

    server = MagicMock()
    server.sendmail.side_effect = refused_sendmail
    with patch.object(email_mod.smtplib, "SMTP", return_value=server), \
         patch.object(email_mod.smtplib, "SMTP_SSL", return_value=server), \
         patch.object(email_mod.ssl, "create_default_context"), \
         patch.object(email_mod.time, "sleep") as mock_sleep:
        results = email_mod.send_alert_emails(
            ["c@example.com"], "Central", "delay", "msg", "2026-01-01T00:00:00Z",
        )
    assert "failed" in results["c@example.com"]
    assert attempts["n"] == 1, "a permanent failure must not be retried"
    mock_sleep.assert_not_called()


def test_email_transient_outage_that_never_recovers_fails_after_bounded_retries():
    """FAILURE case (transient, exhausted): a sustained outage still
    eventually fails, after - and only after - the configured number
    of attempts, never retried forever."""
    attempts = {"n": 0}

    def always_disconnect(*args, **kwargs):
        attempts["n"] += 1
        raise smtplib.SMTPServerDisconnected("still down")

    server = MagicMock()
    server.sendmail.side_effect = always_disconnect
    with patch.object(email_mod.smtplib, "SMTP", return_value=server), \
         patch.object(email_mod.smtplib, "SMTP_SSL", return_value=server), \
         patch.object(email_mod.ssl, "create_default_context"), \
         patch.object(email_mod.time, "sleep"):
        results = email_mod.send_alert_emails(
            ["d@example.com"], "Central", "delay", "msg", "2026-01-01T00:00:00Z",
        )
    assert "failed" in results["d@example.com"]
    assert attempts["n"] == settings.NOTIFICATION_SEND_MAX_ATTEMPTS


def test_email_result_has_exactly_one_outcome_per_recipient():
    """Idempotency guardrail: no matter how many attempts a recipient
    took internally, send_alert_emails reports exactly one outcome per
    recipient - the caller (alert_service._log_results) can never be
    handed two rows for the same send."""
    attempts = {"n": 0}

    def flaky_sendmail(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise smtplib.SMTPServerDisconnected("connection dropped")
        return None

    server = MagicMock()
    server.sendmail.side_effect = flaky_sendmail
    with patch.object(email_mod.smtplib, "SMTP", return_value=server), \
         patch.object(email_mod.smtplib, "SMTP_SSL", return_value=server), \
         patch.object(email_mod.ssl, "create_default_context"), \
         patch.object(email_mod.time, "sleep"):
        results = email_mod.send_alert_emails(
            ["e@example.com"], "Central", "delay", "msg", "2026-01-01T00:00:00Z",
        )
    assert list(results.keys()) == ["e@example.com"]
    assert len(results) == 1


# ---------------------------------------------------------------------
# Fix #2b: SMS transient-failure retry (success / failure / retry)
# ---------------------------------------------------------------------

from app.core import sms as sms_mod


@pytest.fixture(autouse=True)
def _sms_settings(monkeypatch):
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "sid")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "token")
    monkeypatch.setattr(settings, "TWILIO_FROM_NUMBER", "+10000000000")


def _http_error(code, reason="error", body=b"detail"):
    return urllib.error.HTTPError(
        "https://api.twilio.com/x", code, reason, {}, io.BytesIO(body)
    )


def test_sms_success_sends_on_first_attempt():
    with patch.object(sms_mod.urllib.request, "urlopen", return_value=_fake_smtp_response(201)):
        results = sms_mod.send_alert_sms(["+11111111111"], "Central", "delay", "msg")
    assert results == {"+11111111111": "sent"}


def test_sms_transient_network_outage_recovers_via_retry():
    """RETRY case: Twilio is briefly unreachable (connection refused),
    then the retry succeeds - the SMS is NOT lost."""
    attempts = {"n": 0}

    def flaky_urlopen(req, timeout=15):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise urllib.error.URLError("connection refused")
        return _fake_smtp_response(201)

    with patch.object(sms_mod.urllib.request, "urlopen", side_effect=flaky_urlopen), \
         patch.object(sms_mod.time, "sleep") as mock_sleep:
        results = sms_mod.send_alert_sms(["+12222222222"], "Central", "delay", "msg")
    assert results == {"+12222222222": "sent"}
    assert attempts["n"] == 2
    mock_sleep.assert_called_once()


def test_sms_permanent_failure_is_not_retried():
    """FAILURE case (permanent): a 400 (bad request/invalid number) is
    never retried."""
    attempts = {"n": 0}

    def bad_request(req, timeout=15):
        attempts["n"] += 1
        raise _http_error(400, body=b"Invalid phone number")

    with patch.object(sms_mod.urllib.request, "urlopen", side_effect=bad_request), \
         patch.object(sms_mod.time, "sleep") as mock_sleep:
        results = sms_mod.send_alert_sms(["+13333333333"], "Central", "delay", "msg")
    assert "failed" in results["+13333333333"]
    assert attempts["n"] == 1
    mock_sleep.assert_not_called()


def test_sms_provider_5xx_outage_that_never_recovers_fails_after_bounded_retries():
    """FAILURE case (transient, exhausted): a sustained Twilio 503
    still eventually fails, only after the configured number of
    attempts."""
    attempts = {"n": 0}

    def down(req, timeout=15):
        attempts["n"] += 1
        raise _http_error(503, body=b"try again")

    with patch.object(sms_mod.urllib.request, "urlopen", side_effect=down), \
         patch.object(sms_mod.time, "sleep"):
        results = sms_mod.send_alert_sms(["+14444444444"], "Central", "delay", "msg")
    assert "failed" in results["+14444444444"]
    assert attempts["n"] == settings.NOTIFICATION_SEND_MAX_ATTEMPTS


def test_sms_rate_limited_429_recovers_via_retry():
    attempts = {"n": 0}

    def rate_limited(req, timeout=15):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _http_error(429, body=b"rate limited")
        return _fake_smtp_response(201)

    with patch.object(sms_mod.urllib.request, "urlopen", side_effect=rate_limited), \
         patch.object(sms_mod.time, "sleep"):
        results = sms_mod.send_alert_sms(["+15555555555"], "Central", "delay", "msg")
    assert results == {"+15555555555": "sent"}
    assert attempts["n"] == 2


# ---------------------------------------------------------------------
# Fix #1: resolve-alert dispatch idempotency (duplicate notifications)
# ---------------------------------------------------------------------


def _make_fake_alert(is_resolved=False):
    return SimpleNamespace(
        id=42,
        station_id=1,
        is_resolved=is_resolved,
        resolved_at=None,
        notify_email=True,
        notify_sms=False,
    )


def test_resolve_alert_reports_transition_only_once():
    """A repeated resolve_alert() call for the same already-resolved
    alert must report `just_resolved=False` and must not re-commit or
    re-broadcast - this is what makes the PATCH endpoint idempotent
    under a retried/duplicated request."""
    from app.services import alert_service

    fake_alert = _make_fake_alert(is_resolved=False)
    fake_db = MagicMock()
   
    fake_db.execute.side_effect = [
        SimpleNamespace(rowcount=1),
        SimpleNamespace(rowcount=0),
    ]

    with patch.object(alert_service, "get_alert", return_value=fake_alert), \
         patch.object(alert_service, "_broadcast_alert") as mock_broadcast:
        alert1, just_resolved1 = alert_service.resolve_alert(fake_db, 42)
        assert just_resolved1 is True
        assert alert1.is_resolved is True
        assert fake_db.commit.call_count == 1
        mock_broadcast.assert_called_once()

        # Second call: same (now-resolved) alert object, as a retried
        # request against the DB would see.
        alert2, just_resolved2 = alert_service.resolve_alert(fake_db, 42)
        assert just_resolved2 is False
        assert alert2 is fake_alert
        # No further writes or broadcasts on the repeat call.
        assert fake_db.commit.call_count == 1
        mock_broadcast.assert_called_once()


def test_resolve_endpoint_only_dispatches_on_actual_transition():
    """Router-level guardrail: the PATCH /alerts/{id}/resolve handler
    must gate notification_executor.submit() on `just_resolved`, not
    merely on `notify_on_resolve` - this is the actual fix for the
    duplicate-notification bug (a retried PATCH request no longer
    re-sends the resolution email/SMS/bell notification)."""
    from app.api.v1 import alerts as alerts_router
    from app.schemas.alert import AlertResolve

    fake_alert = _make_fake_alert(is_resolved=False)
    fake_db = MagicMock()
    fake_user = SimpleNamespace(id="user-1")

    with patch.object(
        alerts_router.alert_service, "resolve_alert",
        side_effect=[(fake_alert, True), (fake_alert, False)],
    ), patch.object(alerts_router.notification_dispatch_queue, "enqueue_and_submit") as mock_submit:
        # First call: alert actually transitions -> dispatch exactly once.
        alerts_router.resolve_alert(
            request=_fake_request(),
            alert_id=42,
            payload=AlertResolve(notify_on_resolve=True),
            db=fake_db,
            current_user=fake_user,
        )
        assert mock_submit.call_count == 1

        # Second call (simulating a retried/duplicated request): the
        # alert is already resolved, so no further dispatch happens
        # even though notify_on_resolve is still true.
        alerts_router.resolve_alert(
            request=_fake_request(),
            alert_id=42,
            payload=AlertResolve(notify_on_resolve=True),
            db=fake_db,
            current_user=fake_user,
        )
        assert mock_submit.call_count == 1, (
            "a retried resolve request must not re-dispatch the "
            "resolution notification"
        )
