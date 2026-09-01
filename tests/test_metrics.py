"""Tests for app/core/metrics.py and its wiring into app/main.py,
app/database/database.py and app/core/cache.py.

Grouped to match the two metric families described in
app/core/metrics.py's module docstring:

  1. API latency/error metrics - `route_label()`, and the
     `metrics_middleware` in app/main.py that records
     http_requests_total / http_request_duration_seconds for every
     HTTP request (verified end-to-end via the `client` fixture).

  2. PostgreSQL/Redis failure metrics - db_failures_total /
     redis_failures_total, verified both directly (record_db_failure /
     record_redis_failure) and at their real call sites
     (app/database/database.py's `_record_db_failure` SQLAlchemy
     event listener, app/core/cache.py's `_mark_down`, and
     app/main.py's `db_pool_exhausted_handler`) - specifically
     confirming those call sites only ever pass an exception CLASS
     NAME through, never str()/repr() of the exception, which is what
     keeps connection strings/credentials out of the metric.

None of these tests need a live PostgreSQL or Redis connection: the
call-site tests below invoke the actual production functions directly
with a synthetic exception, rather than trying to provoke a real
connection failure.
"""
import asyncio
import types

import pytest
from prometheus_client.parser import text_string_to_metric_families

from app.core import cache, metrics
from app.database import database as database_module


def _counter_value(counter, **labels) -> float:
    """Current value of one labeled child of a prometheus_client
    Counter/Histogram-child - the standard way to read a metric back
    in-process without going through the /metrics text format."""
    return counter.labels(**labels)._value.get()


def _metrics_text() -> str:
    body, _content_type = metrics.render_latest()
    return body.decode("utf-8")


# --- 1. API latency/error metrics: route_label() ---------------------

class _FakeURL:
    def __init__(self, path: str):
        self.path = path


class _FakeRequest:
    """Minimal stand-in for a Starlette Request - route_label() only
    ever touches .scope, .url.path and .path_params."""

    def __init__(self, path: str, path_params: dict | None = None, matched: bool = True):
        self.scope = {"route": object()} if matched else {}
        self.url = _FakeURL(path)
        self.path_params = path_params or {}


def test_route_label_returns_unmatched_for_a_request_that_never_matched_a_route():
    # scope has no "route" key at all - e.g. a 404 for a path no
    # router registered, or scanner/bot traffic.
    request = _FakeRequest("/totally/made/up/path", matched=False)
    assert metrics.route_label(request) == "unmatched"


def test_route_label_substitutes_matched_path_param_values_with_the_template():
    request = _FakeRequest(
        "/api/v1/stations/42",
        path_params={"station_id": 42},
    )
    assert metrics.route_label(request) == "/api/v1/stations/{station_id}"


def test_route_label_scrubs_a_sensitive_looking_path_param_value():
    # A user id, email, or token that happens to be a path param must
    # never survive into the label - only the route shape should.
    request = _FakeRequest(
        "/api/v1/users/someone@example.com",
        path_params={"user_id": "someone@example.com"},
    )
    label = metrics.route_label(request)
    assert label == "/api/v1/users/{user_id}"
    assert "someone@example.com" not in label


def test_route_label_handles_multiple_path_params():
    request = _FakeRequest(
        "/api/v1/crowd/7/analytics",
        path_params={"station_id": 7},
    )
    assert metrics.route_label(request) == "/api/v1/crowd/{station_id}/analytics"


# --- 1. API latency/error metrics: middleware, via the `client` fixture -

def test_metrics_endpoint_returns_prometheus_text(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    families = {
        family.name
        for family in text_string_to_metric_families(response.text)
    }
    assert "http_requests_total" in families
    assert "http_request_duration_seconds" in families
    assert "db_failures_total" in families
    assert "redis_failures_total" in families


def test_metrics_endpoint_not_exposed_in_openapi_schema(client):
    # app/main.py registers it with include_in_schema=False.
    schema = client.get("/openapi.json").json()
    assert "/metrics" not in schema["paths"]


def test_a_request_increments_http_requests_total_with_route_method_and_status(client):
    before = _counter_value(
        metrics.HTTP_REQUESTS_TOTAL, method="GET", route="/healthz", status="200"
    )
    response = client.get("/healthz")
    assert response.status_code == 200
    after = _counter_value(
        metrics.HTTP_REQUESTS_TOTAL, method="GET", route="/healthz", status="200"
    )
    assert after == before + 1


def test_a_request_records_latency_in_the_duration_histogram(client):
    before = metrics.HTTP_REQUEST_DURATION_SECONDS.labels(
        method="GET", route="/healthz"
    )._count.get()
    client.get("/healthz")
    after = metrics.HTTP_REQUEST_DURATION_SECONDS.labels(
        method="GET", route="/healthz"
    )._count.get()
    assert after == before + 1


def test_an_unmatched_path_is_recorded_under_the_unmatched_label_not_verbatim(client):
    bogus_path = "/this-route-does-not-exist-abc123"
    before = _counter_value(
        metrics.HTTP_REQUESTS_TOTAL, method="GET", route="unmatched", status="404"
    )
    response = client.get(bogus_path)
    assert response.status_code == 404
    after = _counter_value(
        metrics.HTTP_REQUESTS_TOTAL, method="GET", route="unmatched", status="404"
    )
    assert after == before + 1
    # The literal requested path must never become a label value.
    assert bogus_path not in _metrics_text()


def test_scraping_the_metrics_endpoint_does_not_record_itself(client):
    client.get("/metrics")
    client.get("/metrics")
    assert 'route="/metrics"' not in _metrics_text()


# --- 2. PostgreSQL/Redis failure metrics: record_*() directly --------

def test_record_db_failure_increments_the_counter_for_its_reason():
    before = _counter_value(metrics.DB_FAILURES_TOTAL, reason="OperationalError")
    metrics.record_db_failure("OperationalError")
    after = _counter_value(metrics.DB_FAILURES_TOTAL, reason="OperationalError")
    assert after == before + 1


def test_record_redis_failure_increments_the_counter_for_its_reason():
    before = _counter_value(metrics.REDIS_FAILURES_TOTAL, reason="ConnectionError")
    metrics.record_redis_failure("ConnectionError")
    after = _counter_value(metrics.REDIS_FAILURES_TOTAL, reason="ConnectionError")
    assert after == before + 1


# --- 2. PostgreSQL/Redis failure metrics: real call sites -------------
#
# These call the actual production functions (not a reimplementation)
# with a synthetic exception carrying an obviously sensitive message,
# to prove the call site only ever forwards the exception's CLASS NAME
# into the metric - never str()/repr(exc), which for a real DB/Redis
# connectivity error can contain the connection string, host, port, or
# credentials.

class _SimulatedOperationalError(Exception):
    pass


class _SimulatedRedisConnectionError(Exception):
    pass


def test_db_handle_error_listener_records_only_the_exception_class_name():
    sensitive_message = (
        "connection to server failed: postgresql://metroflow_user:"
        "S3cretP@ssw0rd@db.internal.example.com:5432/metroflow"
    )
    fake_context = types.SimpleNamespace(
        original_exception=_SimulatedOperationalError(sensitive_message)
    )

    before = _counter_value(
        metrics.DB_FAILURES_TOTAL, reason="_SimulatedOperationalError"
    )
    database_module._record_db_failure(fake_context)
    after = _counter_value(
        metrics.DB_FAILURES_TOTAL, reason="_SimulatedOperationalError"
    )

    assert after == before + 1
    rendered = _metrics_text()
    assert "_SimulatedOperationalError" in rendered
    assert "S3cretP@ssw0rd" not in rendered
    assert "db.internal.example.com" not in rendered


def test_cache_mark_down_records_only_the_exception_class_name():
    sensitive_message = (
        "Error connecting to redis://:S3cretRedisP@ss@cache.internal.example.com:6379/0"
    )
    exc = _SimulatedRedisConnectionError(sensitive_message)

    before = _counter_value(
        metrics.REDIS_FAILURES_TOTAL, reason="_SimulatedRedisConnectionError"
    )
    cache._mark_down(exc)
    after = _counter_value(
        metrics.REDIS_FAILURES_TOTAL, reason="_SimulatedRedisConnectionError"
    )

    assert after == before + 1
    rendered = _metrics_text()
    assert "_SimulatedRedisConnectionError" in rendered
    assert "S3cretRedisP@ss" not in rendered
    assert "cache.internal.example.com" not in rendered


def test_db_pool_exhausted_handler_records_pool_exhausted_reason_and_returns_503():
    # Imported lazily so this file doesn't need app.main at module
    # import time for the tests above that don't need it.
    from app.main import db_pool_exhausted_handler
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    fake_request = types.SimpleNamespace(url=types.SimpleNamespace(path="/api/v1/schedule"))
    exc = SATimeoutError("QueuePool limit reached, connection timed out")

    before = _counter_value(metrics.DB_FAILURES_TOTAL, reason="pool_exhausted")
    response = asyncio.run(db_pool_exhausted_handler(fake_request, exc))
    after = _counter_value(metrics.DB_FAILURES_TOTAL, reason="pool_exhausted")

    assert after == before + 1
    assert response.status_code == 503


# --- Cross-cutting: labels are bounded, fixed-vocabulary strings ------

def test_failure_reason_labels_never_contain_url_like_or_whitespace_content():
    """Belt-and-suspenders check on whatever reasons have actually been
    recorded so far in this test run (by the tests above): every
    reason label used on db_failures_total/redis_failures_total should
    look like a short identifier (an exception class name or
    "pool_exhausted"), never something containing "://", "@", or a
    space - the shape a leaked connection string or credential would
    have."""
    for family in text_string_to_metric_families(_metrics_text()):
        if family.name not in ("db_failures_total", "redis_failures_total"):
            continue
        for sample in family.samples:
            reason = sample.labels.get("reason")
            if reason is None:
                continue
            assert "://" not in reason
            assert "@" not in reason
            assert " " not in reason
