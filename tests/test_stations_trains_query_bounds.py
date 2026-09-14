"""Regression coverage for the stations/trains list endpoints staying
bounded at the service layer, independent of the FastAPI route's own
`Query(..., ge=1, le=MAX_*_LIMIT)` validation (app/api/v1/station.py,
app/api/v1/trains.py). The route-level Query bounds already reject an
out-of-range `limit` with a 422 before it reaches the service, but the
service functions (station_service.list_stations, train_service.
list_trains) also clamp internally - same defense-in-depth pattern as
every other list endpoint (alerts, notifications, schedules,
enquiries, users, news; see test_pagination_limits.py and
test_admin_list_pagination.py). This file adds the same style of
direct, mocked-db regression test for the two list endpoints that
weren't already covered by name.
"""
from unittest.mock import MagicMock

from app.services import station_service
from app.services import train_service


# --- station_service.list_stations ------------------------------------

def _stations_chain(db: MagicMock):
    """station_service.list_stations always applies
    .options(joinedload(...)) before any optional city/state filter,
    then .order_by(...) - this is the chain point .offset()/.limit()
    are called on when no city/state is given."""
    return db.query.return_value.options.return_value.order_by.return_value


def test_list_stations_clamps_oversized_limit_and_paginates():
    db = MagicMock()
    chain = _stations_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = [
        MagicMock(metro_lines=[]) for _ in range(station_service.MAX_STATIONS_LIMIT)
    ]

    result = station_service.list_stations(db, limit=10_000_000, offset=0)

    chain.offset.assert_called_once_with(0)
    chain.offset.return_value.limit.assert_called_once_with(station_service.MAX_STATIONS_LIMIT)
    assert len(result) == station_service.MAX_STATIONS_LIMIT


def test_list_stations_default_limit_used_when_not_specified():
    db = MagicMock()
    chain = _stations_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    station_service.list_stations(db)

    chain.offset.assert_called_once_with(0)
    chain.offset.return_value.limit.assert_called_once_with(station_service.DEFAULT_STATIONS_LIMIT)


def test_list_stations_rejects_negative_offset():
    db = MagicMock()
    chain = _stations_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    station_service.list_stations(db, offset=-50)

    chain.offset.assert_called_once_with(0)


def test_list_stations_zero_limit_falls_back_to_default():
    """station_service.list_stations clamps via `limit or DEFAULT_...`
    (same as train_service.list_trains below), so a falsy 0 is treated
    like "not specified" and uses the default page size - existing,
    unchanged behaviour, just pinned here alongside the other bounds
    checks so a future edit can't silently turn it into an unbounded
    fetch."""
    db = MagicMock()
    chain = _stations_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    station_service.list_stations(db, limit=0)
    chain.offset.return_value.limit.assert_called_with(station_service.DEFAULT_STATIONS_LIMIT)


def test_list_stations_negative_limit_floors_to_one():
    db = MagicMock()
    chain = _stations_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    station_service.list_stations(db, limit=-5)
    chain.offset.return_value.limit.assert_called_with(1)


def test_list_stations_city_filter_still_applied_alongside_paging():
    """Existing city filter behaviour must survive the pagination
    clamp unchanged."""
    db = MagicMock()
    filtered_chain = db.query.return_value.options.return_value.filter.return_value.order_by.return_value
    filtered_chain.offset.return_value.limit.return_value.all.return_value = []

    station_service.list_stations(db, city="Kolkata")

    db.query.return_value.options.return_value.filter.assert_called_once()
    filtered_chain.offset.return_value.limit.assert_called_once_with(station_service.DEFAULT_STATIONS_LIMIT)


# --- train_service.list_trains ------------------------------------------

def _trains_chain(db: MagicMock):
    """train_service.list_trains (no state filter) chains
    .order_by(...) straight off db.query(Train)."""
    return db.query.return_value.order_by.return_value


def test_list_trains_clamps_oversized_limit_and_paginates():
    db = MagicMock()
    chain = _trains_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = [
        MagicMock() for _ in range(train_service.MAX_TRAINS_LIMIT)
    ]

    result = train_service.list_trains(db, limit=10_000_000, offset=0)

    chain.offset.assert_called_once_with(0)
    chain.offset.return_value.limit.assert_called_once_with(train_service.MAX_TRAINS_LIMIT)
    assert len(result) == train_service.MAX_TRAINS_LIMIT


def test_list_trains_default_limit_used_when_not_specified():
    db = MagicMock()
    chain = _trains_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    train_service.list_trains(db)

    chain.offset.assert_called_once_with(0)
    chain.offset.return_value.limit.assert_called_once_with(train_service.DEFAULT_TRAINS_LIMIT)


def test_list_trains_rejects_negative_offset():
    db = MagicMock()
    chain = _trains_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    train_service.list_trains(db, offset=-50)

    chain.offset.assert_called_once_with(0)


def test_list_trains_zero_limit_falls_back_to_default():
    """Same `limit or DEFAULT_...` clamp shape as
    station_service.list_stations above - 0 is falsy and is treated as
    "not specified"."""
    db = MagicMock()
    chain = _trains_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    train_service.list_trains(db, limit=0)
    chain.offset.return_value.limit.assert_called_with(train_service.DEFAULT_TRAINS_LIMIT)


def test_list_trains_negative_limit_floors_to_one():
    db = MagicMock()
    chain = _trains_chain(db)
    chain.offset.return_value.limit.return_value.all.return_value = []

    train_service.list_trains(db, limit=-5)
    chain.offset.return_value.limit.assert_called_with(1)


# --- route-level bound (defense in depth on top of the service clamp) ---

def test_stations_route_rejects_limit_above_max_with_422(client):
    response = client.get("/api/v1/stations/", params={"limit": station_service.MAX_STATIONS_LIMIT + 1})
    assert response.status_code == 422


def test_trains_route_rejects_limit_above_max_with_422(client):
    response = client.get("/api/v1/trains/", params={"limit": train_service.MAX_TRAINS_LIMIT + 1})
    assert response.status_code == 422
