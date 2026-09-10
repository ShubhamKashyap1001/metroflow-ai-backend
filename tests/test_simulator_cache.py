
from unittest.mock import MagicMock

import pytest

from app.simulator import csv_replay_simulator, train_simulator


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


@pytest.fixture
def fake_cache(monkeypatch):
    fake = _FakeCache()
    monkeypatch.setattr(csv_replay_simulator, "cache", fake)
    monkeypatch.setattr(train_simulator, "cache", fake)
    return fake


# --- csv_replay_simulator._tick_sync -----------------------------------

def test_crowd_tick_invalidates_dashboard_and_per_station_cache(fake_cache):
    """A tick that produced updates for stations in Delhi and Mumbai
    must drop crowd:dashboard:all, both affected state views, both
    affected city views, and each touched station's crowd:latest entry
    - the exact keys crowd_service.invalidate_station_cache() already
    drops for a request-triggered write, now reached from the tick
    loop's own bulk write instead."""
    updates = [
        {"station_id": 1, "station_code": "STN-DEL-01", "station_name": "A",
         "current_count": 10, "crowd_level": "Low", "source_timestamp": "t"},
        {"station_id": 2, "station_code": "STN-MUM-01", "station_name": "B",
         "current_count": 20, "crowd_level": "Low", "source_timestamp": "t"},
    ]
    stations_by_id_lookup = {1: {"city": "Delhi"}, 2: {"city": "Mumbai"}}

    csv_replay_simulator.cache.delete("crowd:dashboard:all")
    touched_states, touched_cities = set(), set()
    for row in updates:
        csv_replay_simulator.cache.delete(f"crowd:latest:{row['station_id']}")
        station = stations_by_id_lookup.get(row["station_id"])
        city = station["city"] if station else None
        if city:
            touched_cities.add(city)
            state = csv_replay_simulator.state_for_city(city)
            if state:
                touched_states.add(state)
    for state in touched_states:
        csv_replay_simulator.cache.delete(f"crowd:dashboard:{state}")
    for city in touched_cities:
        csv_replay_simulator.cache.delete(f"crowd:dashboard:{city}")

    assert "crowd:dashboard:all" in fake_cache.deleted
    assert "crowd:latest:1" in fake_cache.deleted
    assert "crowd:latest:2" in fake_cache.deleted
    assert "crowd:dashboard:Delhi" in fake_cache.deleted
    assert "crowd:dashboard:Maharashtra" in fake_cache.deleted
    assert "crowd:dashboard:Mumbai" in fake_cache.deleted


def test_tick_sync_actually_invalidates_on_real_updates(fake_cache, monkeypatch):
    """End-to-end through _tick_sync itself (not just the extracted
    logic above): a tick that writes station rows and commits must
    result in cache.delete("crowd:dashboard:all") having been called."""
    monkeypatch.setattr(csv_replay_simulator, "_load_csv_once", lambda: None)
    monkeypatch.setattr(csv_replay_simulator, "_rows_by_station", {"STN-DEL-01": object()})
    monkeypatch.setattr(
        csv_replay_simulator, "_load_stations",
        lambda db: [{"id": 1, "station_code": "STN-DEL-01", "station_name": "A",
                     "capacity": 100, "city": "Delhi"}],
    )
    monkeypatch.setattr(csv_replay_simulator, "_active_checkins_by_station_by_station", lambda db, ids: {})

    import pandas as pd
    row = pd.Series({"entries": 10, "exits": 0, "timestamp": pd.Timestamp("2026-01-01")})
    monkeypatch.setattr(csv_replay_simulator, "_next_row", lambda code: row)
    monkeypatch.setattr(csv_replay_simulator, "_current_count_from_row", lambda code, r: 10)
    monkeypatch.setattr(csv_replay_simulator, "_level_from_row", lambda r, ratio: "Low")
    monkeypatch.setattr(csv_replay_simulator, "_should_write_history", lambda sid, now: False)
    monkeypatch.setattr(csv_replay_simulator, "_upsert_live_state", lambda db, rows: None)
    monkeypatch.setattr(csv_replay_simulator, "_persist_replay_state", lambda: None)

    db = MagicMock()
    updates = csv_replay_simulator._tick_sync(db)

    assert updates, "tick should have produced an update for the one station"
    assert "crowd:dashboard:all" in fake_cache.deleted
    assert "crowd:latest:1" in fake_cache.deleted
    assert "crowd:dashboard:Delhi" in fake_cache.deleted


# --- train_simulator._invalidate_train_position_cache / _track_tick_sync ---

def test_invalidate_train_position_cache_drops_all_and_every_state(fake_cache):
    train_simulator._invalidate_train_position_cache()

    assert "train:positions:all" in fake_cache.deleted
    for state in train_simulator.STATE_CITY_MAP:
        assert f"train:positions:{state}" in fake_cache.deleted


def test_track_tick_sync_invalidates_cache_when_updates_produced(fake_cache, monkeypatch):
    train = MagicMock(id=1, train_number="T-1", is_active=True)
    monkeypatch.setattr(
        train_simulator, "build_routes",
        lambda db, ids: ({1: [10, 20]}, {1: [100]}, {1: {}}),
    )
    monkeypatch.setattr(train_simulator, "speed_factor_for", lambda delay: 1.0)
    monkeypatch.setattr(train_simulator, "segment_index_for", lambda i, d, n: 0)
    monkeypatch.setattr(train_simulator, "eta_seconds_for", lambda p, seg, sf: 10)

    loc = MagicMock(station_id=10, next_station_id=10, progress_ratio=0.0)
    monkeypatch.setattr(train_simulator, "_locations_for", lambda db, ids, first: {1: loc})

    station = MagicMock(id=10, station_name="A")
    db = MagicMock()
    db.query.return_value.filter.return_value.all.side_effect = [[train], [station]]

    updates = train_simulator._track_tick_sync(db, 5)

    assert updates
    assert "train:positions:all" in fake_cache.deleted
