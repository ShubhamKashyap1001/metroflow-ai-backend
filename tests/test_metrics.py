
import asyncio
import types

import pytest
from prometheus_client.parser import text_string_to_metric_families

from app.core import cache, metrics
from app.database import database as database_module

def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()

def _metrics_text() -> str:
    body, _content_type = metrics.render_latest()
    return body.decode("utf-8")

class _FakeURL:
    def __init__(self, path: str):
        self.path = path

class _FakeRequest:

    def __init__(self, path: str, path_params: dict | None = None, matched: bool = True):
        self.scope = {"route": object()} if matched else {}
        self.url = _FakeURL(path)
        self.path_params = path_params or {}

def test_route_label_returns_unmatched_for_a_request_that_never_matched_a_route():

    request = _FakeRequest("/totally/made/up/path", matched=False)
    assert metrics.route_label(request) == "unmatched"

def test_route_label_substitutes_matched_path_param_values_with_the_template():
    request = _FakeRequest(
        "/api/v1/stations/42",
        path_params={"station_id": 42},
    )
    assert metrics.route_label(request) == "/api/v1/stations/{station_id}"

def test_route_label_scrubs_a_sensitive_looking_path_param_value():

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

def test_metrics_endpoint_returns_prometheus_text(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    families = {
        family.name
        for family in text_string_to_metric_families(response.text)
    }

    assert "http_requests" in families
    assert "http_request_duration_seconds" in families
    assert "db_failures" in families
    assert "redis_failures" in families

def test_metrics_endpoint_not_exposed_in_openapi_schema(client):

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

    def _observation_count() -> float:
        histogram = metrics.HTTP_REQUEST_DURATION_SECONDS
        for family in histogram.collect():
            for sample in family.samples:
                if (
                    sample.name.endswith("_count")
                    and sample.labels.get("method") == "GET"
                    and sample.labels.get("route") == "/healthz"
                ):
                    return sample.value
        return 0.0

    before = _observation_count()
    client.get("/healthz")
    after = _observation_count()
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

    assert bogus_path not in _metrics_text()

def test_scraping_the_metrics_endpoint_does_not_record_itself(client):
    client.get("/metrics")
    client.get("/metrics")
    assert 'route="/metrics"' not in _metrics_text()

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

    from app.main import db_pool_exhausted_handler
    from sqlalchemy.exc import TimeoutError as SATimeoutError

    fake_request = types.SimpleNamespace(url=types.SimpleNamespace(path="/api/v1/schedule"))
    exc = SATimeoutError("QueuePool limit reached, connection timed out")

    before = _counter_value(metrics.DB_FAILURES_TOTAL, reason="pool_exhausted")
    response = asyncio.run(db_pool_exhausted_handler(fake_request, exc))
    after = _counter_value(metrics.DB_FAILURES_TOTAL, reason="pool_exhausted")

    assert after == before + 1
    assert response.status_code == 503

def test_failure_reason_labels_never_contain_url_like_or_whitespace_content():
    for family in text_string_to_metric_families(_metrics_text()):
        if family.name not in ("db_failures", "redis_failures"):
            continue
        for sample in family.samples:
            reason = sample.labels.get("reason")
            if reason is None:
                continue
            assert "://" not in reason
            assert "@" not in reason
            assert " " not in reason
