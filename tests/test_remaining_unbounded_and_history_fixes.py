"""Follow-up to Phase 9 (tests/test_pagination_limits.py) and the
users/schedules pagination fixes: closes the two *remaining* gaps of
the same two classes of bug -

  1. An unbounded API response: news_service.list_news ran `.all()`
     with no limit/offset at all - GET /news pulled the entire news
     table into one JSON response, growing unbounded as announcements
     accumulate.

  2. Expensive list/history queries reached via an unbounded time
     window instead of a raw limit: crowd_service.get_inflow_outflow /
     get_inflow_outflow_bulk (used by get_station_monitor) and
     analytics_service.traffic_analysis_report /
     passenger_flow_overview all accept a caller-supplied `hours`
     window with no upper bound - an oversized value degrades into
     scanning/materializing the entire ever-growing crowd_logs table.

Same style/reason as tests/test_pagination_limits.py: pure unit tests
against the service layer via MagicMock/patch, since this sandbox has
no network access to install fastapi/sqlalchemy/redis for a real
TestClient + Postgres run. Each test simulates a "large dataset" by
having the mocked query return however many rows the *clamped*
bound implies, then asserts the clamp actually reached the query
(offset/limit call args, or the `since` cutoff derived from `hours`),
not just that the function returned something.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app.services import analytics_service
from app.services import crowd_service
from app.services import news_service


# --- news_service.list_news (unbounded API response) -----------------------

def _query_chain(db: MagicMock):
    """Return the mock object representing db.query(...).order_by(...)
    - the point at which .offset()/.limit() are chained on in
    list_news, matching the existing pagination test helper."""
    return db.query.return_value.order_by.return_value


def test_list_news_clamps_oversized_limit_and_paginates():
    """A caller asking for an absurd page size (simulating someone
    trying to pull the whole news table in one shot - e.g. years of
    accumulated announcements) must be capped at MAX_NEWS_LIMIT, and
    the cap must actually reach the DB query (.limit()), not just get
    silently ignored."""
    db = MagicMock()
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = [
        MagicMock() for _ in range(news_service.MAX_NEWS_LIMIT)
    ]

    result = news_service.list_news(db, limit=10_000_000, offset=0)

    chain.offset.assert_called_once_with(0)
    chain.offset.return_value.limit.assert_called_once_with(news_service.MAX_NEWS_LIMIT)
    assert len(result) == news_service.MAX_NEWS_LIMIT


def test_list_news_default_limit_used_when_not_specified():
    """Regression guard: previously list_news took no limit/offset
    parameters at all and ran a bare `.all()` - this asserts the new
    default page size is actually applied even when the caller (e.g.
    the existing frontend, which never passed these params) doesn't
    specify one."""
    db = MagicMock()
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    news_service.list_news(db)

    chain.offset.assert_called_once_with(0)
    chain.offset.return_value.limit.assert_called_once_with(news_service.DEFAULT_NEWS_LIMIT)


def test_list_news_rejects_negative_offset():
    db = MagicMock()
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    news_service.list_news(db, offset=-50)

    chain.offset.assert_called_once_with(0)


def test_list_news_zero_or_negative_limit_falls_back_to_at_least_one():
    db = MagicMock()
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    news_service.list_news(db, limit=0)
    chain.offset.return_value.limit.assert_called_with(1)

    news_service.list_news(db, limit=-5)
    chain.offset.return_value.limit.assert_called_with(1)


def test_list_news_include_inactive_filter_still_applied_alongside_paging():
    """Regression guard: the existing is_active filter (admins/
    operators opting into drafts via include_inactive=True) must keep
    working exactly as before now that paging has been added."""
    db = MagicMock()
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    news_service.list_news(db, include_inactive=False)
    db.query.return_value.filter.assert_called_once()

    db.reset_mock()
    chain = _query_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []
    news_service.list_news(db, include_inactive=True)
    db.query.return_value.filter.assert_not_called()


# --- crowd_service.get_inflow_outflow / get_inflow_outflow_bulk ------------
# (expensive history query reached via an unbounded `hours` window)

def test_get_inflow_outflow_clamps_absurd_hours_window():
    """A caller passing hours=87_600_000 ("10 thousand years",
    simulating an attempt to force a full scan of the ever-growing
    crowd_logs table) must have the window clamped to
    MAX_HISTORY_WINDOW_HOURS - surfaced here via the returned
    window_hours field, which reports the value actually used."""
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

    result = crowd_service.get_inflow_outflow(db, station_id=1, hours=87_600_000)

    assert result["window_hours"] == crowd_service.MAX_HISTORY_WINDOW_HOURS


def test_get_inflow_outflow_normal_request_is_unaffected():
    """The frontend's real usage (hours=24 by default, up to 72 on the
    Reports panel) must behave exactly as before - the cap should
    never bite for reasonable values."""
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

    result = crowd_service.get_inflow_outflow(db, station_id=1, hours=24)

    assert result["window_hours"] == 24


def test_get_inflow_outflow_bulk_clamps_absurd_hours_before_building_the_time_window():
    """Verified directly at the point the time window is built: patch
    `timedelta` itself (wrapping the real implementation so the
    function under test still works) and assert it's invoked with the
    CLAMPED value - proof the absurd input never reaches `since`
    construction unclamped, regardless of any SQLAlchemy/query-layer
    details."""
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

    with patch.object(crowd_service, "timedelta", wraps=timedelta) as mock_timedelta:
        crowd_service.get_inflow_outflow_bulk(db, station_ids=[1, 2], hours=87_600_000)

    mock_timedelta.assert_called_once_with(hours=crowd_service.MAX_HISTORY_WINDOW_HOURS)


def test_get_inflow_outflow_bulk_normal_request_is_unaffected():
    db = MagicMock()
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

    with patch.object(crowd_service, "timedelta", wraps=timedelta) as mock_timedelta:
        crowd_service.get_inflow_outflow_bulk(db, station_ids=[1, 2], hours=1)

    mock_timedelta.assert_called_once_with(hours=1)


def test_get_inflow_outflow_bulk_empty_station_ids_short_circuits_without_querying():
    """Regression guard: the existing empty-input fast path (no
    stations to report on) must still skip the query entirely."""
    db = MagicMock()
    result = crowd_service.get_inflow_outflow_bulk(db, station_ids=[], hours=999_999)
    assert result == {}
    db.query.assert_not_called()


# --- analytics_service.traffic_analysis_report / passenger_flow_overview --
# (same unbounded-`hours` history-query class, on the Analytics dashboard)

def test_traffic_analysis_report_clamps_absurd_hours_window():
    db = MagicMock()
    db.query.return_value.filter.return_value.group_by.return_value.all.return_value = []
    db.query.return_value.all.return_value = []
    db.query.return_value.filter.return_value.count.return_value = 0

    result = analytics_service.traffic_analysis_report(db, hours=87_600_000)

    assert result["window_hours"] == analytics_service.MAX_HISTORY_WINDOW_HOURS


def test_traffic_analysis_report_normal_request_is_unaffected():
    db = MagicMock()
    db.query.return_value.filter.return_value.group_by.return_value.all.return_value = []
    db.query.return_value.all.return_value = []
    db.query.return_value.filter.return_value.count.return_value = 0

    result = analytics_service.traffic_analysis_report(db, hours=24)

    assert result["window_hours"] == 24


def test_passenger_flow_overview_clamps_absurd_hours_window():
    """passenger_flow_overview is the more dangerous of the two - it
    pulls raw per-row CrowdLog data with `.all()` (no row limit at
    all), so an unbounded `hours` window here means materializing
    every historical row in Python, not just an unbounded SQL
    aggregate."""
    db = MagicMock()
    station_row = MagicMock(id=1, station_name="Central", capacity=100)
    db.query.return_value.filter.return_value.all.return_value = [station_row]
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
    db.query.return_value.join.return_value.filter.return_value.all.return_value = []

    result = analytics_service.passenger_flow_overview(db, hours=87_600_000, top_n=8)

    assert result["window_hours"] == analytics_service.MAX_HISTORY_WINDOW_HOURS


def test_passenger_flow_overview_normal_request_is_unaffected():
    db = MagicMock()
    station_row = MagicMock(id=1, station_name="Central", capacity=100)
    db.query.return_value.filter.return_value.all.return_value = [station_row]
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
    db.query.return_value.join.return_value.filter.return_value.all.return_value = []

    result = analytics_service.passenger_flow_overview(db, hours=24, top_n=8)

    assert result["window_hours"] == 24
