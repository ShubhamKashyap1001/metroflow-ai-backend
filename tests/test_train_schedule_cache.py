from datetime import time
from unittest.mock import MagicMock

import pytest

from app.enums.schedule_status import ScheduleStatus
from app.schemas.train import TrainCreate, TrainUpdate
from app.schemas.train_schedule import TrainScheduleCreate, TrainScheduleUpdate
from app.services import schedule_service, train_service, train_tracking
from app.utils.geo import STATE_CITY_MAP

class _FakeCache:

    def __init__(self):
        self.deleted: list[str] = []

    def delete(self, key):
        self.deleted.append(key)

    def get_json(self, key):
        return None

    def set_json(self, key, value, ttl_seconds=None):
        pass

class _FakeManager:

    def __init__(self):
        self.notified: list[tuple[str, dict]] = []

    def notify(self, event, data):
        self.notified.append((event, data))

@pytest.fixture
def fake_cache(monkeypatch):
    fake = _FakeCache()
    monkeypatch.setattr(schedule_service, "cache", fake)
    monkeypatch.setattr(train_service, "cache", fake)
    monkeypatch.setattr(train_tracking, "cache", fake)
    return fake

@pytest.fixture
def fake_manager(monkeypatch):
    fake = _FakeManager()
    monkeypatch.setattr(schedule_service, "manager", fake)
    return fake

@pytest.fixture(autouse=True)
def _reset_route_cache_module_state():
    train_tracking._routes_cache = {}
    train_tracking._segment_seconds_cache = {}
    train_tracking._routes_cache_at = 0.0
    yield
    train_tracking._routes_cache = {}
    train_tracking._segment_seconds_cache = {}
    train_tracking._routes_cache_at = 0.0

def _mock_db(existing=None, get_map=None):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing
    db.get.side_effect = lambda model, id_: (get_map or {}).get(id_)
    return db

def test_create_schedule_invalidates_default_page_caches(fake_cache, fake_manager):
    db = _mock_db(get_map={})
    payload = TrainScheduleCreate(
        train_id=1,
        station_id=2,
        arrival_time=time(8, 0),
        departure_time=time(8, 2),
        platform_number=1,
    )

    schedule_service.create_schedule(db, payload)

    default_page = f"{schedule_service.DEFAULT_SCHEDULE_LIST_LIMIT}:0"
    assert f"schedule:list:None:None:None:None:{default_page}" in fake_cache.deleted
    assert f"schedule:list:None:1:None:None:{default_page}" in fake_cache.deleted
    assert f"schedule:list:2:None:None:None:{default_page}" in fake_cache.deleted
    assert f"schedule:peak:None:None:{default_page}" in fake_cache.deleted
    assert f"schedule:delayed:None:None:{default_page}" in fake_cache.deleted

def test_create_schedule_broadcasts_schedule_update(fake_cache, fake_manager):
    train = MagicMock(train_number="T-100")
    station = MagicMock(station_name="Central")
    db = _mock_db(get_map={1: train, 2: station})
    payload = TrainScheduleCreate(
        train_id=1,
        station_id=2,
        arrival_time=time(8, 0),
        departure_time=time(8, 2),
        platform_number=1,
    )

    schedule = schedule_service.create_schedule(db, payload)

    assert len(fake_manager.notified) == 1
    event, data = fake_manager.notified[0]
    assert event == "schedule_update"
    assert data["schedule_id"] == schedule.id
    assert data["train_number"] == "T-100"
    assert data["station_name"] == "Central"
    assert data["platform_number"] == 1

def test_update_schedule_invalidates_and_broadcasts(fake_cache, fake_manager, monkeypatch):
    existing = MagicMock(
        id=42,
        train_id=1,
        station_id=2,
        arrival_time=time(8, 0),
        departure_time=time(8, 2),
        platform_number=1,
        status=ScheduleStatus.ON_TIME,
        delay_minutes=0,
        frequency_minutes=10,
        is_peak_hour=False,
    )
    monkeypatch.setattr(schedule_service, "get_schedule", lambda db, sid: existing)
    db = _mock_db(get_map={1: MagicMock(train_number="T-1"), 2: MagicMock(station_name="S-1")})

    schedule_service.update_schedule(db, 42, TrainScheduleUpdate(platform_number=3))

    assert existing.platform_number == 3
    default_page = f"{schedule_service.DEFAULT_SCHEDULE_LIST_LIMIT}:0"
    assert f"schedule:list:None:None:None:None:{default_page}" in fake_cache.deleted
    assert len(fake_manager.notified) == 1
    assert fake_manager.notified[0][0] == "schedule_update"

def test_handle_delay_and_adjust_frequency_still_invalidate_only_once(fake_cache, fake_manager, monkeypatch):
    existing = MagicMock(
        id=7,
        train_id=1,
        station_id=2,
        arrival_time=time(9, 0),
        delay_minutes=0,
        status=ScheduleStatus.ON_TIME,
    )
    monkeypatch.setattr(schedule_service, "get_schedule", lambda db, sid: existing)
    monkeypatch.setattr(schedule_service.notification_service, "create_notification", lambda *a, **k: None)
    db = _mock_db(get_map={1: MagicMock(train_number="T-1"), 2: MagicMock(city="Delhi", station_name="S-1")})

    schedule_service.handle_delay(db, 7, schedule_service.DelayUpdate(delay_minutes=2))

    default_page = f"{schedule_service.DEFAULT_SCHEDULE_LIST_LIMIT}:0"
    assert f"schedule:peak:2:None:{default_page}" in fake_cache.deleted

    assert any(event == "delay_alert" for event, _ in fake_manager.notified)

def test_create_train_invalidates_all_position_cache_keys(fake_cache):
    db = _mock_db(existing=None)
    payload = TrainCreate(train_number="T-999", capacity=500)

    train_service.create_train(db, payload)

    assert "train:positions:all" in fake_cache.deleted
    for state in STATE_CITY_MAP:
        assert f"train:positions:{state}" in fake_cache.deleted

def test_update_train_invalidates_position_cache(fake_cache, monkeypatch):
    train = MagicMock(id=5)
    monkeypatch.setattr(train_service, "get_train", lambda db, tid: train)
    db = _mock_db()

    train_service.update_train(db, 5, TrainUpdate(capacity=600))

    assert train.capacity == 600
    assert "train:positions:all" in fake_cache.deleted
    assert f"train:positions:Delhi" in fake_cache.deleted

def test_create_schedule_invalidates_stale_route_cache(fake_cache, fake_manager):

    train_tracking._routes_cache = {1: [2, 3]}
    train_tracking._segment_seconds_cache = {1: [120]}
    train_tracking._routes_cache_at = train_tracking._time.monotonic()

    db = _mock_db(get_map={})
    payload = TrainScheduleCreate(
        train_id=1,
        station_id=2,
        arrival_time=time(8, 0),
        departure_time=time(8, 2),
        platform_number=1,
    )

    schedule_service.create_schedule(db, payload)

    assert "schedule:routes" in fake_cache.deleted

    assert train_tracking._routes_cache == {}
    assert train_tracking._segment_seconds_cache == {}
    assert train_tracking._routes_cache_at == 0.0

def test_update_schedule_invalidates_stale_route_cache(fake_cache, fake_manager, monkeypatch):
    existing = MagicMock(
        id=42,
        train_id=1,
        station_id=2,
        arrival_time=time(8, 0),
        departure_time=time(8, 2),
        platform_number=1,
        status=ScheduleStatus.ON_TIME,
        delay_minutes=0,
        frequency_minutes=10,
        is_peak_hour=False,
    )
    monkeypatch.setattr(schedule_service, "get_schedule", lambda db, sid: existing)
    db = _mock_db(get_map={1: MagicMock(train_number="T-1"), 2: MagicMock(station_name="S-1")})

    train_tracking._routes_cache = {1: [2, 3]}
    train_tracking._segment_seconds_cache = {1: [120]}
    train_tracking._routes_cache_at = train_tracking._time.monotonic()

    schedule_service.update_schedule(db, 42, TrainScheduleUpdate(arrival_time=time(8, 30)))

    assert "schedule:routes" in fake_cache.deleted
    assert train_tracking._routes_cache == {}

def test_build_routes_rebuilds_from_db_immediately_after_invalidation(fake_cache, monkeypatch):
    train_tracking._routes_cache = {1: [2, 3]}
    train_tracking._segment_seconds_cache = {1: [999]}
    train_tracking._routes_cache_at = train_tracking._time.monotonic()

    train_tracking.invalidate_route_cache()
    assert "schedule:routes" in fake_cache.deleted

    refreshed = MagicMock()
    monkeypatch.setattr(train_tracking, "_refresh_route_cache", lambda db: refreshed())

    db = MagicMock()
    train_tracking.build_routes(db, [1])

    refreshed.assert_called_once()

def test_handle_delay_does_not_touch_route_cache(fake_cache, fake_manager, monkeypatch):
    existing = MagicMock(
        id=7, train_id=1, station_id=2, arrival_time=time(9, 0),
        delay_minutes=0, status=ScheduleStatus.ON_TIME,
    )
    monkeypatch.setattr(schedule_service, "get_schedule", lambda db, sid: existing)
    monkeypatch.setattr(schedule_service.notification_service, "create_notification", lambda *a, **k: None)
    db = _mock_db(get_map={1: MagicMock(train_number="T-1"), 2: MagicMock(city="Delhi", station_name="S-1")})

    schedule_service.handle_delay(db, 7, schedule_service.DelayUpdate(delay_minutes=2))

    assert "schedule:routes" not in fake_cache.deleted
