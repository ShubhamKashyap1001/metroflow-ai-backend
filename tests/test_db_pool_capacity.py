import pytest

from app.database.database import check_pool_capacity

def test_safe_configuration_does_not_raise():
    cluster_ceiling, safe_capacity = check_pool_capacity(
        pool_size=15, max_overflow=25, web_concurrency=1,
        reserve=10, max_connections=100,
    )
    assert cluster_ceiling == 40
    assert safe_capacity == 90

def test_exactly_at_the_boundary_does_not_raise():
    cluster_ceiling, safe_capacity = check_pool_capacity(
        pool_size=10, max_overflow=0, web_concurrency=1,
        reserve=0, max_connections=10,
    )
    assert cluster_ceiling == safe_capacity == 10

def test_multi_worker_overcommit_raises():
    with pytest.raises(RuntimeError, match="refusing to start"):
        check_pool_capacity(
            pool_size=15, max_overflow=25, web_concurrency=4,
            reserve=10, max_connections=100,
        )

def test_single_worker_can_still_overcommit_a_small_server():
    with pytest.raises(RuntimeError):
        check_pool_capacity(
            pool_size=15, max_overflow=25, web_concurrency=1,
            reserve=10, max_connections=20,
        )

def test_error_message_names_the_actual_offending_settings():
    with pytest.raises(RuntimeError) as exc_info:
        check_pool_capacity(
            pool_size=15, max_overflow=25, web_concurrency=3,
            reserve=10, max_connections=100,
        )
    message = str(exc_info.value)
    for expected in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "WEB_CONCURRENCY", "120", "90"):
        assert expected in message, f"expected {expected!r} in error message: {message}"

def test_reserve_is_subtracted_before_comparing():

    with pytest.raises(RuntimeError):
        check_pool_capacity(
            pool_size=15, max_overflow=25, web_concurrency=1,
            reserve=10, max_connections=45,
        )

    check_pool_capacity(
        pool_size=15, max_overflow=25, web_concurrency=1,
        reserve=5, max_connections=45,
    )

def test_does_not_mutate_or_depend_on_global_settings():
    check_pool_capacity(
        pool_size=5, max_overflow=5, web_concurrency=1,
        reserve=0, max_connections=100,
    )
    with pytest.raises(RuntimeError):
        check_pool_capacity(
            pool_size=500, max_overflow=500, web_concurrency=1,
            reserve=0, max_connections=100,
        )

    check_pool_capacity(
        pool_size=5, max_overflow=5, web_concurrency=1,
        reserve=0, max_connections=100,
    )
