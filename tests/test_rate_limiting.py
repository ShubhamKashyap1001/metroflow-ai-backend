
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
    """Every test in this file authenticates via the dev bypass
    (any Bearer token is treated as an email - see
    app.core.security._get_or_create_profile_by_email) so we don't
    need a real Supabase-issued JWT to reach write/AI/auth endpoints."""
    monkeypatch.setattr(settings, "AUTH_DISABLED", True)
    monkeypatch.setattr(settings, "DEBUG", True)


def _auth_header() -> dict:
    """A fresh identity + a fresh spoofed source IP per test.

    The shared limiter's key_func (slowapi.util.get_ipaddr) buckets by
    *client IP*, honoring X-Forwarded-For - and every call through
    FastAPI's TestClient otherwise looks like it comes from the same
    loopback address. Without a unique X-Forwarded-For per test, every
    test in this file would fight over the same handful of buckets and
    the outcome would depend on execution order. The fresh dev-bypass
    email also keeps each test's own DB rows (journeys, profiles)
    isolated from the others."""
    return {
        "Authorization": f"Bearer ratelimit-test-{uuid.uuid4()}@example.com",
        "X-Forwarded-For": f"10.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 256}.{uuid.uuid4().int % 256}",
    }


def _limit_count(limit_str: str) -> int:
    """'30/minute' -> 30."""
    return int(limit_str.split("/")[0])


# --------------------------------------------------------------------
# 1. Rate limits actually trigger a 429 once the threshold is crossed
# --------------------------------------------------------------------

@requires_redis
def test_auth_me_returns_429_past_its_limit(client: TestClient):
    headers = _auth_header()
    n = _limit_count(AUTH_LIMIT)

    statuses = [client.get("/api/v1/auth/me", headers=headers).status_code for _ in range(n)]
    assert all(s == 200 for s in statuses), statuses

    # One more over the limit must be rejected.
    over_limit = client.get("/api/v1/auth/me", headers=headers)
    assert over_limit.status_code == 429


@requires_redis
def test_write_endpoint_returns_429_past_its_limit(client: TestClient):
    """/checkin is decorated with WRITE_LIMIT. The payload references
    stations that don't exist, so most calls will 404/422 - that's
    fine, the rate-limit check inside @limiter.limit runs before the
    route body, so it still counts and still fires at the threshold."""
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


# --------------------------------------------------------------------
# 2. 429 response shape - a client can actually parse and act on it
# --------------------------------------------------------------------

@requires_redis
def test_429_response_has_useful_shape(client: TestClient):
    headers = _auth_header()
    n = _limit_count(AUTH_LIMIT)

    for _ in range(n):
        client.get("/api/v1/auth/me", headers=headers)

    response = client.get("/api/v1/auth/me", headers=headers)
    assert response.status_code == 429
    # slowapi's default handler returns a JSON body (not an empty 429)
    # and a Retry-After header a well-behaved client can back off on.
    body = response.json()
    assert body, "429 response body should not be empty"
    assert "Retry-After" in response.headers or "retry-after" in {
        k.lower() for k in response.headers
    }


# --------------------------------------------------------------------
# 3. Multi-worker behavior: two independent Limiter instances (each
#    simulating a separate uvicorn worker process) must share the same
#    counters because they point at the same Redis-backed storage.
# --------------------------------------------------------------------

@requires_redis
def test_limits_are_shared_across_separate_limiter_instances():
    """This is the actual regression test for the multi-worker bug:
    before the fix, `Limiter()` defaulted to in-process memory storage,
    so two separate instances (~= two worker processes) would each
    enforce their own independent quota - a client could get 2x the
    intended allowance by alternating between them. With Redis-backed
    storage_uri, both instances read/write the same counters."""
    key = f"test-client-{uuid.uuid4()}"

    def fake_key_func(request):
        return key

    worker_a = Limiter(key_func=fake_key_func, storage_uri=settings.REDIS_URL)
    worker_b = Limiter(key_func=fake_key_func, storage_uri=settings.REDIS_URL)

    # Use the public `limits` strategy surface both Limiter instances
    # wrap, so this test isn't coupled to slowapi's private attribute
    # names beyond what's necessary to drive two independent instances
    # against the same storage.
    from limits import parse
    from limits.strategies import FixedWindowRateLimiter

    limit = parse("5/minute")
    strategy_a = FixedWindowRateLimiter(worker_a._storage)
    strategy_b = FixedWindowRateLimiter(worker_b._storage)

    # Worker A consumes 3 of the 5 allowed hits.
    for _ in range(3):
        assert strategy_a.hit(limit, key)

    # Worker B (separate instance, same Redis) should only have 2 left,
    # not a fresh 5 - proving the counters are shared, not per-instance.
    assert strategy_b.hit(limit, key)
    assert strategy_b.hit(limit, key)
    assert not strategy_b.hit(limit, key), (
        "worker B was able to exceed the combined 5/minute quota - "
        "storage is not actually shared between instances"
    )


def test_shared_limiter_redis_connection_has_socket_timeouts():
    """Phase 17D regression test: `app.core.rate_limit.limiter`'s own
    ongoing per-request Redis calls (not just the one-time
    `_redis_reachable()` startup probe) must be bounded, or a Redis
    that stops responding *after* startup (network partition, wedged
    server) hangs every rate-limited request forever instead of
    failing fast.

    Doesn't require a live Redis - this only inspects the connection
    kwargs the storage layer was built with, so it also catches a
    regression on a dev box where Redis isn't running (the limiter
    falls back to in-memory storage in that case; this test only
    applies when it didn't).
    """
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
    """The dev-auth-bypass path (see app.core.security) always creates
    a fresh identity as UserRole.PASSENGER. /admin/* requires
    ADMIN/OPERATOR, so tests that exercise it promote the just-created
    row directly in the DB (same deterministic uuid5(email) id the
    bypass itself uses), then bust the profile cache so the next
    request doesn't keep serving the stale cached PASSENGER role for
    up to AUTH_USER_CACHE_TTL_SECONDS."""
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
    """Sanity check that ADMIN_LIMIT (the strictest tier) is wired up
    and shared-storage-backed the same way as the others."""
    headers = _auth_header()
    email = headers["Authorization"].removeprefix("Bearer ")

    # First call both creates the dev-bypass user and gets us a real
    # baseline response once they're promoted to ADMIN below.
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


# --------------------------------------------------------------------
# 4. WebSocket traffic must NOT be rate limited
# --------------------------------------------------------------------

def test_websocket_not_rate_limited_by_default_limits(client: TestClient):
    """Send more messages than DEFAULT_LIMIT over a single WS
    connection and confirm none of it is throttled - SlowAPIMiddleware
    only wraps "http" scope ASGI calls (see app/core/rate_limit.py's
    module docstring), so a persistent WebSocket connection is never
    in its path at all, regardless of how many frames are exchanged."""
    from app.core.rate_limit import DEFAULT_LIMIT

    n_messages = _limit_count(DEFAULT_LIMIT) + 20  # comfortably over the HTTP default

    with client.websocket_connect("/ws/monitor", headers=_auth_header()) as websocket:
        for _ in range(n_messages):
            websocket.send_text('{"type": "ping"}')
            reply = websocket.receive_text()
            assert "pong" in reply


def test_websocket_handshake_unaffected_by_http_rate_limit_state(client: TestClient):
    """Exhaust the global HTTP default limit first, then confirm a
    brand new WebSocket connection still succeeds - proving the two
    are on entirely separate counters (WS isn't touched by
    SlowAPIMiddleware) rather than the WS route just happening to have
    quota left over from a shared bucket."""
    from app.core.rate_limit import DEFAULT_LIMIT

    headers = _auth_header()
    n = _limit_count(DEFAULT_LIMIT)
    for _ in range(n + 5):
        client.get("/healthz", headers=headers)  # undecorated -> only DEFAULT_LIMIT applies

    with client.websocket_connect("/ws/monitor", headers=headers) as websocket:
        websocket.send_text('{"type": "ping"}')
        reply = websocket.receive_text()
        assert "pong" in reply
