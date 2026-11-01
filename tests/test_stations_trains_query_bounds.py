from unittest.mock import MagicMock

from app.services import station_service
from app.services import train_service

def _stations_chain(db: MagicMock):
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
    db = MagicMock()
    filtered_chain = db.query.return_value.options.return_value.filter.return_value.order_by.return_value
    filtered_chain.offset.return_value.limit.return_value.all.return_value = []

    station_service.list_stations(db, city="Kolkata")

    db.query.return_value.options.return_value.filter.assert_called_once()
    filtered_chain.offset.return_value.limit.assert_called_once_with(station_service.DEFAULT_STATIONS_LIMIT)

def _trains_chain(db: MagicMock):
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

def test_stations_route_rejects_limit_above_max_with_422(client):
    response = client.get("/api/v1/stations/", params={"limit": station_service.MAX_STATIONS_LIMIT + 1})
    assert response.status_code == 422

def test_trains_route_rejects_limit_above_max_with_422(client):
    response = client.get("/api/v1/trains/", params={"limit": train_service.MAX_TRAINS_LIMIT + 1})
    assert response.status_code == 422
