
import uuid

import pytest
import redis
from fastapi.testclient import TestClient
from slowapi import Limiter

from app.core.config import settings
from app.core.rate_limit import ADMIN_LIMIT, AI_LIMIT, AUTH_LIMIT, WRITE_LIMIT

def _redis_up() -> bool:
    try:
        return bool(redis.from_url(settings.REDIS_URL, socket_connect_timeout=1).ping())
    except Exception:
        return False

requires_redis = pytest.mark.skipif(not _redis_up(), reason="Redis not reachable")

@pytest.fixture(autouse=True)
def _dev_auth_bypass(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_DISABLED", True)
    monkeypatch.setattr(settings, "DEBUG", True)

def _auth_header() -> dict:
    return {
        "Authorization": f"Bearer ratelimit-test-{uuid.uuid4()}@example.com",
        "X-Forwarded-For": f"10.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 256}",
    }

def _limit_count(limit_str: str) -> int:
    return int(limit_str.split("/")[0])

@requires_redis
def test_auth_me_returns_429_past_its_limit(client: TestClient):
    headers = _auth_header()
    n = _limit_count(AUTH_LIMIT)

    statuses = [client.get("/api/v1/auth/me", headers=headers).status_code for _ in range(n)]
    assert all(s == 200 for s in statuses), statuses

    over_limit = client.get("/api/v1/auth/me", headers=headers)
    assert over_limit.status_code == 429

@requires_redis
def test_write_endpoint_returns_429_past_its_limit(client: TestClient):
    headers = _auth_header()
    n = _limit_count(WRITE_LIMIT)
    payload = {"source_station_id": 999_999_990, "destination_station_id": 999_999_991}

    statuses = [
        client.post("/api/v1/checkin/", json=payload, headers=headers).status_code
        for _ in range(n)
    ]
    assert 429 not in statuses, statuses

    over_limit = client.post("/api/v1/checkin/", json=payload, headers=headers)
    assert over_limit.status_code == 429

@requires_redis
def test_ai_prediction_endpoint_returns_429_past_its_limit(client: TestClient):
    headers = _auth_header()
    n = _limit_count(AI_LIMIT)

    statuses = [
        client.get("/api/v1/predictions/crowd/metrics", headers=headers).status_code
        for _ in range(n)
    ]
    assert 429 not in statuses, statuses

    over_limit = client.get("/api/v1/predictions/crowd/metrics", headers=headers)
    assert over_limit.status_code == 429

@requires_redis
def test_429_response_has_useful_shape(client: TestClient):
    headers = _auth_header()
    n = _limit_count(AUTH_LIMIT)

    for _ in range(n):
        client.get("/api/v1/auth/me", headers=headers)

    response = client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 429

    body = response.json()
    assert body, "429 response body should not be empty"
    assert "Retry-After" in response.headers or "retry-after" in {
        k.lower() for k in response.headers
    }

@requires_redis
def test_limits_are_shared_across_separate_limiter_instances():
    key = f"test-client-{uuid.uuid4()}"

    def fake_key_func(request):
        return key

    worker_a = Limiter(key_func=fake_key_func, storage_uri=settings.REDIS_URL)
    worker_b = Limiter(key_func=fake_key_func, storage_uri=settings.REDIS_URL)

    from limits import parse
    from limits.strategies import FixedWindowRateLimiter

    limit = parse("5/minute")
    strategy_a = FixedWindowRateLimiter(worker_a._storage)
    strategy_b = FixedWindowRateLimiter(worker_b._storage)

    for _ in range(3):
        assert strategy_a.hit(limit, key)

    assert strategy_b.hit(limit, key)
    assert strategy_b.hit(limit, key)
    assert not strategy_b.hit(limit, key), (
        "worker B was able to exceed the combined 5/minute quota - "
        "storage is not actually shared between instances"
    )

def test_shared_limiter_redis_connection_has_socket_timeouts():
    from app.core.rate_limit import limiter

    storage = limiter._storage
    pool = getattr(getattr(storage, "storage", storage), "connection_pool", None)
    if pool is None:
        pytest.skip("limiter fell back to in-memory storage (Redis unreachable at import)")

    conn_kwargs = pool.connection_kwargs
    assert conn_kwargs.get("socket_connect_timeout") is not None, (
        "rate limiter's Redis connection has no socket_connect_timeout - "
        "a stuck TCP handshake would hang every rate-limited request "
        "indefinitely"
    )
    assert conn_kwargs.get("socket_timeout") is not None, (
        "rate limiter's Redis connection has no socket_timeout - a "
        "connected-but-unresponsive Redis would hang every "
        "rate-limited request indefinitely"
    )

def _promote_dev_user_to_admin(email: str) -> None:
    import uuid as _uuid

    from app.core.security import invalidate_user_cache
    from app.database.session import SessionLocal
    from app.enums.user_role import UserRole
    from app.models.user_profile import UserProfile

    user_id = _uuid.uuid5(_uuid.NAMESPACE_DNS, email.strip().lower())
    db = SessionLocal()
    try:
        user = db.get(UserProfile, user_id)
        assert user is not None, "expected the dev-bypass user to already exist"
        user.role = UserRole.ADMIN
        db.commit()
    finally:
        db.close()
    invalidate_user_cache(user_id)

@requires_redis
def test_admin_endpoint_uses_stricter_shared_limit(client: TestClient):
    headers = _auth_header()
    email = headers["Authorization"].removeprefix("Bearer ")

    client.get("/api/v1/auth/me", headers=headers)
    _promote_dev_user_to_admin(email)

    n = _limit_count(ADMIN_LIMIT)
    statuses = [
        client.post("/api/v1/admin/simulator/start", headers=headers).status_code
        for _ in range(n)
    ]
    assert 429 not in statuses, statuses

    over_limit = client.post("/api/v1/admin/simulator/start", headers=headers)
    assert over_limit.status_code == 429

def test_websocket_not_rate_limited_by_default_limits(client: TestClient):
    from app.core.rate_limit import DEFAULT_LIMIT

    n_messages = _limit_count(DEFAULT_LIMIT) + 20  

    with client.websocket_connect("/ws/monitor", headers=_auth_header()) as websocket:
        for _ in range(n_messages):
            websocket.send_text('{"type": "ping"}')
            reply = websocket.receive_text()
            assert "pong" in reply

def test_websocket_handshake_unaffected_by_http_rate_limit_state(client: TestClient):
    from app.core.rate_limit import DEFAULT_LIMIT

    headers = _auth_header()
    n = _limit_count(DEFAULT_LIMIT)
    for _ in range(n + 5):
        client.get("/healthz", headers=headers)  

    with client.websocket_connect("/ws/monitor", headers=headers) as websocket:
        websocket.send_text('{"type": "ping"}')
        reply = websocket.receive_text()
        assert "pong" in reply
