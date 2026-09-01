"""Response-size / server-side-cost regression tests for the
train/schedule list endpoints (schedule_service.py) and the
analytics/user/enquiry list endpoints (enquiry_service.py,
app/api/v1/users.py).

These close the same gap tests/test_pagination_limits.py already
covers for alerts/notifications/predictions (Phase 9), but for the
three read paths that were never given equivalent test coverage even
though the service-layer fix (DEFAULT_*_LIMIT / MAX_*_LIMIT +
hard-clamped offset/limit) was already applied:

  - schedule_service.list_schedules / peak_hour_schedules /
    delayed_schedules - the Dispatch Board's most-hit reads.
  - enquiry_service.list_enquiries - the "My Enquiries" list and the
    admin "manage enquiries" queue.
  - app/api/v1/users.py::get_users - the admin "User Management" list.

Same style as test_pagination_limits.py: pure unit tests against the
service layer (or, for get_users, the route function directly) with
the DB mocked out via MagicMock, since this sandbox has no network
access to stand up a real Postgres. Each test asserts the clamp
actually reaches the query (the .limit()/.offset() call args), not
just that the function returns something - that's the part that
protects the server from an unbounded fetch.
"""
from unittest.mock import MagicMock, patch

from app.api.v1 import users as users_router
from app.enums.user_role import UserRole
from app.services import enquiry_service, schedule_service


# --- schedule_service.list_schedules / peak_hour_schedules / delayed_schedules ---

def _schedule_query_chain(db: MagicMock):
    """The mock object list_schedules/peak_hour_schedules/delayed_schedules
    chain .offset()/.limit() onto - i.e. db.query(...).filter(...)... up
    to .order_by(...). Since these three build the filter chain slightly
    differently, we just stub every attribute access to return the same
    chained mock (a MagicMock does this by default), then anchor on
    `.order_by.return_value` explicitly. Cache is bypassed via `cache.get_json`
    returning None (Redis unreachable / no-op default), so every call here
    falls through to `_compute()` and actually exercises the query builder.
    """
    return db.query.return_value.filter.return_value.order_by.return_value


def test_list_schedules_clamps_oversized_limit(monkeypatch):
    monkeypatch.setattr(schedule_service.cache, "get_json", lambda *a, **k: None)
    monkeypatch.setattr(schedule_service.cache, "set_json", lambda *a, **k: None)
    db = MagicMock()
    chain = db.query.return_value.order_by.return_value
    chain.offset.return_value.limit.return_value.all.return_value = [
        MagicMock() for _ in range(schedule_service.MAX_SCHEDULE_LIST_LIMIT)
    ]

    result = schedule_service.list_schedules(db, limit=10_000_000, offset=0)

    chain.offset.assert_called_once_with(0)
    chain.offset.return_value.limit.assert_called_once_with(
        schedule_service.MAX_SCHEDULE_LIST_LIMIT
    )
    assert len(result) == schedule_service.MAX_SCHEDULE_LIST_LIMIT


def test_list_schedules_default_limit_used_when_not_specified(monkeypatch):
    monkeypatch.setattr(schedule_service.cache, "get_json", lambda *a, **k: None)
    monkeypatch.setattr(schedule_service.cache, "set_json", lambda *a, **k: None)
    db = MagicMock()
    chain = db.query.return_value.order_by.return_value
    chain.offset.return_value.limit.return_value.all.return_value = []

    schedule_service.list_schedules(db)

    chain.offset.assert_called_once_with(0)
    chain.offset.return_value.limit.assert_called_once_with(
        schedule_service.DEFAULT_SCHEDULE_LIST_LIMIT
    )


def test_list_schedules_rejects_negative_offset(monkeypatch):
    monkeypatch.setattr(schedule_service.cache, "get_json", lambda *a, **k: None)
    monkeypatch.setattr(schedule_service.cache, "set_json", lambda *a, **k: None)
    db = MagicMock()
    chain = db.query.return_value.order_by.return_value
    chain.offset.return_value.limit.return_value.all.return_value = []

    schedule_service.list_schedules(db, offset=-50)

    chain.offset.assert_called_once_with(0)


def test_peak_hour_schedules_clamps_oversized_limit(monkeypatch):
    monkeypatch.setattr(schedule_service.cache, "get_json", lambda *a, **k: None)
    monkeypatch.setattr(schedule_service.cache, "set_json", lambda *a, **k: None)
    db = MagicMock()
    chain = _schedule_query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = [
        MagicMock() for _ in range(schedule_service.MAX_SCHEDULE_LIST_LIMIT)
    ]

    result = schedule_service.peak_hour_schedules(db, limit=999_999)

    chain.offset.return_value.limit.assert_called_once_with(
        schedule_service.MAX_SCHEDULE_LIST_LIMIT
    )
    assert len(result) == schedule_service.MAX_SCHEDULE_LIST_LIMIT


def test_delayed_schedules_clamps_oversized_limit(monkeypatch):
    monkeypatch.setattr(schedule_service.cache, "get_json", lambda *a, **k: None)
    monkeypatch.setattr(schedule_service.cache, "set_json", lambda *a, **k: None)
    db = MagicMock()
    chain = _schedule_query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = [
        MagicMock() for _ in range(schedule_service.MAX_SCHEDULE_LIST_LIMIT)
    ]

    result = schedule_service.delayed_schedules(db, limit=999_999)

    chain.offset.return_value.limit.assert_called_once_with(
        schedule_service.MAX_SCHEDULE_LIST_LIMIT
    )
    assert len(result) == schedule_service.MAX_SCHEDULE_LIST_LIMIT


def test_delayed_schedules_zero_limit_floors_to_one(monkeypatch):
    monkeypatch.setattr(schedule_service.cache, "get_json", lambda *a, **k: None)
    monkeypatch.setattr(schedule_service.cache, "set_json", lambda *a, **k: None)
    db = MagicMock()
    chain = _schedule_query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    schedule_service.delayed_schedules(db, limit=0)
    chain.offset.return_value.limit.assert_called_with(1)


# --- enquiry_service.list_enquiries ----------------------------------------

def _staff_user():
    user = MagicMock()
    user.role = UserRole.ADMIN
    user.id = "admin-1"
    return user


def test_list_enquiries_clamps_oversized_limit():
    db = MagicMock()
    chain = db.query.return_value.options.return_value.order_by.return_value
    chain.offset.return_value.limit.return_value.all.return_value = [
        MagicMock() for _ in range(enquiry_service.MAX_ENQUIRIES_LIMIT)
    ]

    result = enquiry_service.list_enquiries(db, _staff_user(), limit=1_000_000)

    chain.offset.return_value.limit.assert_called_once_with(
        enquiry_service.MAX_ENQUIRIES_LIMIT
    )
    assert len(result) == enquiry_service.MAX_ENQUIRIES_LIMIT


def test_list_enquiries_default_limit_used_when_not_specified():
    db = MagicMock()
    chain = db.query.return_value.options.return_value.order_by.return_value
    chain.offset.return_value.limit.return_value.all.return_value = []

    enquiry_service.list_enquiries(db, _staff_user())

    chain.offset.assert_called_once_with(0)
    chain.offset.return_value.limit.assert_called_once_with(
        enquiry_service.DEFAULT_ENQUIRIES_LIMIT
    )


def test_list_enquiries_rejects_negative_offset():
    db = MagicMock()
    chain = db.query.return_value.options.return_value.order_by.return_value
    chain.offset.return_value.limit.return_value.all.return_value = []

    enquiry_service.list_enquiries(db, _staff_user(), offset=-10)

    chain.offset.assert_called_once_with(0)


# --- app/api/v1/users.py::get_users -----------------------------------------

def test_get_users_clamps_oversized_limit():
    db = MagicMock()
    chain = db.query.return_value.filter.return_value.order_by.return_value
    chain.offset.return_value.limit.return_value.all.return_value = [
        MagicMock() for _ in range(users_router.MAX_USERS_LIMIT)
    ]

    result = users_router.get_users(limit=5_000_000, offset=0, db=db, current_user=MagicMock())

    chain.offset.return_value.limit.assert_called_once_with(users_router.MAX_USERS_LIMIT)
    assert len(result) == users_router.MAX_USERS_LIMIT


def test_get_users_default_limit_used_when_not_specified():
    db = MagicMock()
    chain = db.query.return_value.filter.return_value.order_by.return_value
    chain.offset.return_value.limit.return_value.all.return_value = []

    users_router.get_users(limit=users_router.DEFAULT_USERS_LIMIT, offset=0, db=db, current_user=MagicMock())

    chain.offset.return_value.limit.assert_called_once_with(users_router.DEFAULT_USERS_LIMIT)


def test_get_users_rejects_negative_offset():
    db = MagicMock()
    chain = db.query.return_value.filter.return_value.order_by.return_value
    chain.offset.return_value.limit.return_value.all.return_value = []

    users_router.get_users(limit=users_router.DEFAULT_USERS_LIMIT, offset=-25, db=db, current_user=MagicMock())

    chain.offset.assert_called_once_with(0)
