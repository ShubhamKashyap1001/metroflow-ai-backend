import pytest

from app.database.database import check_pool_capacity


def test_safe_configuration_does_not_raise():
    """The project's actual defaults (pool_size=15, max_overflow=25,
    web_concurrency=1 -> ceiling 40) against a typical Postgres
    max_connections=100 should pass with room to spare."""
    cluster_ceiling, safe_capacity = check_pool_capacity(
        pool_size=15, max_overflow=25, web_concurrency=1,
        reserve=10, max_connections=100,
    )
    assert cluster_ceiling == 40
    assert safe_capacity == 90


def test_exactly_at_the_boundary_does_not_raise():
    """Ceiling == safe_capacity (not >) must be allowed - this is the
    off-by-one edge every "exceeds capacity" check needs to get right
    in the permissive direction at the exact boundary."""
    cluster_ceiling, safe_capacity = check_pool_capacity(
        pool_size=10, max_overflow=0, web_concurrency=1,
        reserve=0, max_connections=10,
    )
    assert cluster_ceiling == safe_capacity == 10


def test_multi_worker_overcommit_raises():
    """The actual bug this phase targets: a single process's pool
    looks fine (40 connections vs. 100 max_connections), but scaling
    to several worker processes multiplies that ceiling - here 4
    workers x 40 = 160, which blows past a 100-connection server even
    though nothing about DB_POOL_SIZE/DB_MAX_OVERFLOW itself changed."""
    with pytest.raises(RuntimeError, match="refusing to start"):
        check_pool_capacity(
            pool_size=15, max_overflow=25, web_concurrency=4,
            reserve=10, max_connections=100,
        )


def test_single_worker_can_still_overcommit_a_small_server():
    """Even web_concurrency=1 can be unsafe against a small/free-tier
    Postgres instance (e.g. max_connections=20) - this isn't only a
    multi-worker problem, just the one the informational-log-only
    version of this check (Phase 6) couldn't have caught since it
    never actually knew the server's real max_connections."""
    with pytest.raises(RuntimeError):
        check_pool_capacity(
            pool_size=15, max_overflow=25, web_concurrency=1,
            reserve=10, max_connections=20,
        )


def test_error_message_names_the_actual_offending_settings():
    """The error must be immediately actionable - an operator reading
    it should not have to go re-derive which number(s) to change."""
    with pytest.raises(RuntimeError) as exc_info:
        check_pool_capacity(
            pool_size=15, max_overflow=25, web_concurrency=3,
            reserve=10, max_connections=100,
        )
    message = str(exc_info.value)
    for expected in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "WEB_CONCURRENCY", "120", "90"):
        assert expected in message, f"expected {expected!r} in error message: {message}"


def test_reserve_is_subtracted_before_comparing():
    """A configuration that would fit against the server's raw
    max_connections, but not once DB_CONNECTION_RESERVE is set aside
    for migrations/admin/monitoring, must still be rejected - the
    reserve exists precisely so this app's pool never claims the
    server's entire connection budget for itself."""
    # 40 fits under max_connections=45 raw, but not under the
    # reserve-adjusted safe capacity of 45 - 10 = 35.
    with pytest.raises(RuntimeError):
        check_pool_capacity(
            pool_size=15, max_overflow=25, web_concurrency=1,
            reserve=10, max_connections=45,
        )
    # The same ceiling against a server with enough headroom to
    # absorb the reserve too (45 - 5 = 40) is fine.
    check_pool_capacity(
        pool_size=15, max_overflow=25, web_concurrency=1,
        reserve=5, max_connections=45,
    )


def test_does_not_mutate_or_depend_on_global_settings():
    """Pure function contract: calling it twice with different inputs
    must not leak state between calls (e.g. via a module-level
    global) - guards against a future refactor accidentally
    reintroducing a dependency on `app.core.config.settings` here
    instead of the explicit arguments."""
    check_pool_capacity(
        pool_size=5, max_overflow=5, web_concurrency=1,
        reserve=0, max_connections=100,
    )
    with pytest.raises(RuntimeError):
        check_pool_capacity(
            pool_size=500, max_overflow=500, web_concurrency=1,
            reserve=0, max_connections=100,
        )
    # And back to a safe config again - must not still be "poisoned"
    # by the previous call's failure.
    check_pool_capacity(
        pool_size=5, max_overflow=5, web_concurrency=1,
        reserve=0, max_connections=100,
    )
