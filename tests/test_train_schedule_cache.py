from datetime import time
from unittest.mock import MagicMock

import pytest

from app.enums.schedule_status import ScheduleStatus
from app.schemas.train import TrainCreate, TrainUpdate
from app.schemas.train_schedule import TrainScheduleCreate, TrainScheduleUpdate
from app.services import schedule_service, train_service, train_tracking
from app.utils.geo import STATE_CITY_MAP


class _FakeCache:
    """Records every delete() call; get/set are no-ops (always a miss)
    since these tests only care about invalidation on write."""

    def __init__(self):
        self.deleted: list[str] = []

    def delete(self, key):
        self.deleted.append(key)

    def get_json(self, key):
        return None

    def set_json(self, key, value, ttl_seconds=None):
        pass


class _FakeManager:
    """Records every notify() call: (event, payload)."""

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
    """train_tracking keeps a process-local dict/timestamp alongside
    the Redis-backed cache - reset it around every test in this file
    so one test's writes can't leak into the next."""
    train_tracking._routes_cache = {}
    train_tracking._segment_seconds_cache = {}
    train_tracking._routes_cache_at = 0.0
    yield
    train_tracking._routes_cache = {}
    train_tracking._segment_seconds_cache = {}
    train_tracking._routes_cache_at = 0.0


def _mock_db(existing=None, get_map=None):
    """A MagicMock Session good enough for create_*/update_* - `.query(...)
    .filter(...).first()` (duplicate-check) returns `existing`, `.get(Model, id)`
    looks `id` up in `get_map` (dict keyed by id), `.add/.commit/.refresh`
    are no-ops."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing
    db.get.side_effect = lambda model, id_: (get_map or {}).get(id_)
    return db


# --- schedule_service.create_schedule / update_schedule ---------------------

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
    """Guard against a regression the other way: the already-working
    handle_delay/adjust_frequency workflows should be untouched by this
    fix (still exactly the same invalidation shape as before)."""
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
    # handle_delay keeps emitting its own DELAY_ALERT event (unchanged).
    assert any(event == "delay_alert" for event, _ in fake_manager.notified)


# --- train_service.create_train / update_train -------------------------------

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


# --- stale route/segment cache (train_tracking) ------------------------------

def test_create_schedule_invalidates_stale_route_cache(fake_cache, fake_manager):
    """BUGFIX (stale cached data): train_tracking.build_routes() caches
    each train's route/segment-duration shape for
    _ROUTE_CACHE_TTL_SECONDS (5 min). A brand new schedule row changes
    that train's route the moment it's committed - it must not still
    be missing from the Live Train Map / /trains/routes response for
    up to 5 minutes after creation."""
    # Simulate a warm, not-yet-expired route cache for this train, the
    # same way build_routes() would have left it after an earlier read.
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
    # The process-local snapshot must be dropped too, not just the
    # Redis-backed key - otherwise this same process keeps serving the
    # pre-write route until its own TTL window happens to lapse.
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
    """End-to-end within train_tracking: after invalidate_route_cache(),
    the very next build_routes() call for the same train must NOT be
    served from the (now-cleared) process-local snapshot or a stale
    Redis entry - it must recompute from Postgres."""
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
    """delay_minutes is deliberately never part of the route/segment
    cache (train_tracking._delay_by_station_for always reads fresh) -
    handle_delay/adjust_frequency don't change route shape, so they
    should keep NOT invalidating this cache."""
    existing = MagicMock(
        id=7, train_id=1, station_id=2, arrival_time=time(9, 0),
        delay_minutes=0, status=ScheduleStatus.ON_TIME,
    )
    monkeypatch.setattr(schedule_service, "get_schedule", lambda db, sid: existing)
    monkeypatch.setattr(schedule_service.notification_service, "create_notification", lambda *a, **k: None)
    db = _mock_db(get_map={1: MagicMock(train_number="T-1"), 2: MagicMock(city="Delhi", station_name="S-1")})

    schedule_service.handle_delay(db, 7, schedule_service.DelayUpdate(delay_minutes=2))

    assert "schedule:routes" not in fake_cache.deleted
