
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.enums.crowd_level import CrowdLevel
from app.models.crowd_log import CrowdLog
from app.models.station import Station
from app.services import analytics_service
from app.services import crowd_service
from app.services import news_service


# --- news_service.list_news (unbounded API response) -----------------------

def _query_chain(db: MagicMock):
    return db.query.return_value.filter.return_value.order_by.return_value


def test_list_news_clamps_oversized_limit_and_paginates():
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


# --- crowd_service.crowd_flow_aggregate / get_inflow_outflow(_bulk) --------
# (expensive history query reached via an unbounded `hours` window)
#
# RAM FIX (Render Free 512MB): inflow/outflow used to be computed by
# pulling every raw CrowdLog row in the window into Python and walking
# it with a running-previous-count loop - a mocked `.all()` chain could
# stand in for that fine. It's now computed by a single SQL window-
# function + GROUP BY query (crowd_flow_aggregate), so these tests run
# against a real database instead of a mocked query chain - a mock
# can't meaningfully stand in for LAG()/ROW_NUMBER() SQL.

@pytest.fixture
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def _make_station(db: Session, station_code: str, **overrides) -> Station:
    """Uses flush (not commit) - the whole test runs inside one
    transaction the db_session fixture rolls back afterward, so
    repeated test runs never collide on station_code uniqueness."""
    defaults = dict(
        station_code=station_code,
        station_name=f"Test Station {station_code}",
        city="Test City",
        latitude=0.0,
        longitude=0.0,
        capacity=1000,
        is_active=True,
    )
    defaults.update(overrides)
    station = Station(**defaults)
    db.add(station)
    db.flush()
    return station


def _add_log(db: Session, station_id: int, count: int, when: datetime) -> None:
    db.add(CrowdLog(
        station_id=station_id,
        current_count=count,
        crowd_level=CrowdLevel.LOW,
        created_at=when,
    ))
    db.flush()


def test_crowd_flow_aggregate_matches_hand_computed_totals(db_session):
    """The SQL aggregate must produce the exact same inflow/outflow/
    samples/last_count a running-previous-count Python loop over the
    same rows would - only how it's computed changed, not what it
    means."""
    station = _make_station(db_session, "QB-AGG-1")
    now = datetime.now(timezone.utc)
    # 100 -> 150 (+50 in) -> 120 (-30 out) -> 200 (+80 in) -> 180 (-20 out)
    for offset_hours, count in [(5, 100), (4, 150), (3, 120), (2, 200), (1, 180)]:
        _add_log(db_session, station.id, count, now - timedelta(hours=offset_hours))

    aggregate = crowd_service.crowd_flow_aggregate(
        db_session, [station.id], since=now - timedelta(hours=24)
    )

    assert aggregate[station.id] == {
        "inflow": 130,
        "outflow": 50,
        "samples": 5,
        "last_count": 180,
    }


def test_crowd_flow_aggregate_returns_one_row_per_station_regardless_of_sample_count(db_session):
    """RAM-safety guarantee: however many raw rows exist in the window,
    exactly one summarized row per station comes back - the memory
    footprint of the response does not grow with history size."""
    station = _make_station(db_session, "QB-AGG-2")
    now = datetime.now(timezone.utc)
    sample_count = 300
    for i in range(sample_count):
        _add_log(db_session, station.id, i, now - timedelta(minutes=sample_count - i))

    aggregate = crowd_service.crowd_flow_aggregate(
        db_session, [station.id], since=now - timedelta(hours=24)
    )

    assert len(aggregate) == 1
    values = aggregate[station.id]
    assert values["samples"] == sample_count
    assert values["inflow"] == sample_count - 1  # +1 per consecutive step
    assert values["outflow"] == 0
    assert values["last_count"] == sample_count - 1


def test_crowd_flow_aggregate_respects_the_since_cutoff(db_session):
    """Samples older than `since` must not contribute to the totals or
    the sample count - the aggregate query filters in SQL exactly like
    the old Python loop's `created_at >= since` did."""
    station = _make_station(db_session, "QB-AGG-3")
    now = datetime.now(timezone.utc)
    _add_log(db_session, station.id, 50, now - timedelta(hours=10))  # outside window
    _add_log(db_session, station.id, 90, now - timedelta(hours=1, minutes=30))
    _add_log(db_session, station.id, 130, now - timedelta(minutes=30))

    aggregate = crowd_service.crowd_flow_aggregate(
        db_session, [station.id], since=now - timedelta(hours=2)
    )

    values = aggregate[station.id]
    assert values["samples"] == 2
    assert values["inflow"] == 40
    assert values["outflow"] == 0
    assert values["last_count"] == 130


def test_crowd_flow_aggregate_empty_station_ids_short_circuits_without_querying(db_session):
    assert crowd_service.crowd_flow_aggregate(db_session, [], since=datetime.now(timezone.utc)) == {}


def test_get_inflow_outflow_clamps_absurd_hours_window(db_session):
    station = _make_station(db_session, "QB-IO-1")

    result = crowd_service.get_inflow_outflow(db_session, station_id=station.id, hours=87_600_000)

    assert result["window_hours"] == crowd_service.MAX_HISTORY_WINDOW_HOURS
    assert result == {
        "station_id": station.id,
        "window_hours": crowd_service.MAX_HISTORY_WINDOW_HOURS,
        "inflow": 0,
        "outflow": 0,
        "samples": 0,
    }


def test_get_inflow_outflow_normal_request_is_unaffected(db_session):
    station = _make_station(db_session, "QB-IO-2")
    now = datetime.now(timezone.utc)
    _add_log(db_session, station.id, 40, now - timedelta(hours=2))
    _add_log(db_session, station.id, 65, now - timedelta(hours=1))

    result = crowd_service.get_inflow_outflow(db_session, station_id=station.id, hours=24)

    assert result["window_hours"] == 24
    assert result["inflow"] == 25
    assert result["outflow"] == 0
    assert result["samples"] == 2


def test_get_inflow_outflow_bulk_clamps_absurd_hours_before_building_the_time_window(db_session):
    stations = [_make_station(db_session, f"QB-IOB-{i}") for i in range(2)]
    station_ids = [s.id for s in stations]

    with patch.object(crowd_service, "timedelta", wraps=timedelta) as mock_timedelta:
        crowd_service.get_inflow_outflow_bulk(db_session, station_ids=station_ids, hours=87_600_000)

    mock_timedelta.assert_called_once_with(hours=crowd_service.MAX_HISTORY_WINDOW_HOURS)


def test_get_inflow_outflow_bulk_normal_request_is_unaffected(db_session):
    stations = [_make_station(db_session, f"QB-IOB2-{i}") for i in range(2)]
    station_ids = [s.id for s in stations]

    with patch.object(crowd_service, "timedelta", wraps=timedelta) as mock_timedelta:
        result = crowd_service.get_inflow_outflow_bulk(db_session, station_ids=station_ids, hours=1)

    mock_timedelta.assert_called_once_with(hours=1)
    assert set(result.keys()) == set(station_ids)
    for station_id in station_ids:
        assert result[station_id] == {"inflow": 0, "outflow": 0, "samples": 0}


def test_get_inflow_outflow_bulk_computes_per_station_totals_independently(db_session):
    station_a = _make_station(db_session, "QB-IOB3-A")
    station_b = _make_station(db_session, "QB-IOB3-B")
    now = datetime.now(timezone.utc)
    _add_log(db_session, station_a.id, 10, now - timedelta(minutes=30))
    _add_log(db_session, station_a.id, 40, now - timedelta(minutes=10))
    _add_log(db_session, station_b.id, 90, now - timedelta(minutes=30))
    _add_log(db_session, station_b.id, 60, now - timedelta(minutes=10))

    result = crowd_service.get_inflow_outflow_bulk(
        db_session, station_ids=[station_a.id, station_b.id], hours=1
    )

    assert result[station_a.id] == {"inflow": 30, "outflow": 0, "samples": 2}
    assert result[station_b.id] == {"inflow": 0, "outflow": 30, "samples": 2}


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


def test_passenger_flow_overview_clamps_absurd_hours_window(db_session):
    """passenger_flow_overview used to pull raw per-row CrowdLog data
    with `.all()` (no row limit at all), so an unbounded `hours` window
    meant materializing every historical row in Python. It now delegates
    to crowd_service.crowd_flow_aggregate() (SQL-side aggregation), so
    this exercises a real database instead of a mocked query chain."""
    station = _make_station(db_session, "QB-PFO-1")

    result = analytics_service.passenger_flow_overview(db_session, hours=87_600_000, top_n=8)

    assert result["window_hours"] == analytics_service.MAX_HISTORY_WINDOW_HOURS
    assert result["total_inflow"] == 0
    assert result["total_outflow"] == 0


def test_passenger_flow_overview_normal_request_is_unaffected(db_session):
    station_a = _make_station(db_session, "QB-PFO-2-A")
    station_b = _make_station(db_session, "QB-PFO-2-B")
    now = datetime.now(timezone.utc)
    _add_log(db_session, station_a.id, 50, now - timedelta(minutes=20))
    _add_log(db_session, station_a.id, 90, now - timedelta(minutes=5))
    _add_log(db_session, station_b.id, 200, now - timedelta(minutes=20))
    _add_log(db_session, station_b.id, 150, now - timedelta(minutes=5))

    result = analytics_service.passenger_flow_overview(db_session, hours=24, top_n=8)

    assert result["window_hours"] == 24
    assert result["total_inflow"] == 40   # station_a: 90 - 50
    assert result["total_outflow"] == 50  # station_b: 200 - 150
    assert result["net_flow"] == -10

    top_by_id = {row["station_id"]: row for row in result["top_stations"]}
    assert top_by_id[station_a.id]["entries"] == 40
    assert top_by_id[station_b.id]["exits"] == 50

    # avg_predicted_occupancy uses each station's LAST known count
    # within the window (90/1000 and 150/1000), matching the old
    # Python loop's running `last_count_by_station` semantics.
    assert result["avg_predicted_occupancy"] == pytest.approx((0.09 + 0.15) / 2, rel=1e-6)

