"""Render Free resource-usage fix: the simulator and train-tracker stay
ENABLED (the dashboard needs continuously replayed passenger-flow data
and live train status), but both background loops tick every 60s
instead of the old 10s config default / 5s scheduler default. The
already-correct 60s crowd-history cadence is untouched.

These tests cover, in order:
1. ENABLE_SIMULATOR defaults to True.
2. ENABLE_TRAIN_TRACKING defaults to True.
3. Configured simulator interval (app.core.config.settings) is 60s.
4. Configured train tracker interval is 60s.
5. app/main.py's lifespan actually passes those configured intervals
   through to scheduler.start_simulator/start_train_tracker at startup
   - there's no hidden 5s/10s default silently overriding them.
6. No 5-second or 10-second default remains anywhere in the
   simulator/tracker call chain (scheduler defaults + the underlying
   run_forever defaults in csv_replay_simulator/train_simulator).
7. Existing simulator functionality still works (start is idempotent,
   configured/default intervals both thread through correctly).
8. Existing train tracking functionality still works (same, for the
   train tracker loop).
9. CROWD_HISTORY_INTERVAL_SECONDS (retention/history cadence) remains
   60s.
"""

import asyncio
import functools
import inspect
from unittest.mock import AsyncMock

import pytest

import app.main as main_module
from app.core.config import settings
from app.simulator import csv_replay_simulator, scheduler, train_simulator


@pytest.fixture(autouse=True)
def _reset_scheduler_globals(monkeypatch):
    """start_simulator()/start_train_tracker() are no-ops once their
    module-level election singleton is already set, so each test needs
    a clean slate regardless of what earlier tests (or app startup)
    did."""
    monkeypatch.setattr(scheduler, "_crowd_election", None)
    monkeypatch.setattr(scheduler, "_train_election", None)


class _FakeLeaderElection:
    """Records the (name, run_loop) it was constructed with instead of
    actually starting a background asyncio task, so tests can assert on
    what interval_seconds ended up baked into run_loop without any real
    scheduling/Redis/DB machinery running."""

    def __init__(self, name, run_loop, *, lease_seconds=15, poll_seconds=3):
        self.name = name
        self.run_loop = run_loop

    def start(self):
        pass


@pytest.fixture
def fake_leader_election(monkeypatch):
    monkeypatch.setattr(scheduler, "LeaderElection", _FakeLeaderElection)
    return _FakeLeaderElection


# --- 1 & 2. Simulator / train tracker stay enabled by default -----------

def test_simulator_enabled_by_default():
    assert settings.ENABLE_SIMULATOR is True

def test_train_tracking_enabled_by_default():
    assert settings.ENABLE_TRAIN_TRACKING is True


# --- 3 & 4. Configured intervals are 60s ---------------------------------

def test_simulator_interval_setting_is_60_seconds():
    assert settings.SIMULATOR_INTERVAL_SECONDS == 60

def test_train_tracker_interval_setting_is_60_seconds():
    assert settings.TRAIN_TRACK_INTERVAL_SECONDS == 60


# --- Scheduler function defaults are 60s too -----------------------------

def test_start_simulator_default_is_60_seconds():
    default = inspect.signature(scheduler.start_simulator).parameters["interval_seconds"].default
    assert default == 60

def test_start_train_tracker_default_is_60_seconds():
    default = inspect.signature(scheduler.start_train_tracker).parameters["interval_seconds"].default
    assert default == 60


# --- 7 & 8. Configured/default values are threaded through correctly ----
#            (= "existing simulator/train-tracking functionality still
#            works")

def test_start_simulator_threads_configured_interval_into_run_loop(fake_leader_election):
    scheduler.start_simulator(object(), settings.SIMULATOR_INTERVAL_SECONDS)

    election = scheduler._crowd_election
    assert isinstance(election, _FakeLeaderElection)
    assert isinstance(election.run_loop, functools.partial)
    # run_loop == functools.partial(run_crowd_forever, session_factory, interval_seconds)
    assert election.run_loop.args[-1] == 60

def test_start_train_tracker_threads_configured_interval_into_run_loop(fake_leader_election):
    scheduler.start_train_tracker(object(), settings.TRAIN_TRACK_INTERVAL_SECONDS)

    election = scheduler._train_election
    assert isinstance(election, _FakeLeaderElection)
    assert isinstance(election.run_loop, functools.partial)
    assert election.run_loop.args[-1] == 60

def test_start_simulator_uses_its_own_default_when_called_without_interval(fake_leader_election):
    """Existing simulator functionality still works when a caller
    relies on the function default (no interval_seconds passed) -
    and that default is the new 60s value, not the old 5s one."""
    scheduler.start_simulator(object())

    assert scheduler._crowd_election.run_loop.args[-1] == 60

def test_start_train_tracker_uses_its_own_default_when_called_without_interval(fake_leader_election):
    """Existing train tracking functionality still works when a caller
    relies on the function default."""
    scheduler.start_train_tracker(object())

    assert scheduler._train_election.run_loop.args[-1] == 60

def test_start_simulator_is_idempotent_once_already_started(fake_leader_election):
    """Existing simulator functionality still works: a second call
    while already running must not spin up a second election/loop."""
    scheduler.start_simulator(object(), 60)
    first_election = scheduler._crowd_election

    scheduler.start_simulator(object(), 60)

    assert scheduler._crowd_election is first_election

def test_start_train_tracker_is_idempotent_once_already_started(fake_leader_election):
    """Existing train tracking functionality still works: a second call
    while already running must not spin up a second election/loop."""
    scheduler.start_train_tracker(object(), 60)
    first_election = scheduler._train_election

    scheduler.start_train_tracker(object(), 60)

    assert scheduler._train_election is first_election


# --- 5. app/main.py's lifespan passes the configured intervals through --

def test_lifespan_passes_configured_intervals_to_scheduler(monkeypatch):
    """The FastAPI startup path (app/main.py's lifespan) must call
    start_simulator/start_train_tracker with settings.SIMULATOR_INTERVAL_SECONDS
    / settings.TRAIN_TRACK_INTERVAL_SECONDS - not some other hardcoded or
    shared value - so the 60s production configuration is what the
    background loops actually run at."""
    calls = {}

    def fake_start_simulator(session_factory, interval_seconds):
        calls["simulator"] = interval_seconds

    def fake_start_train_tracker(session_factory, interval_seconds):
        calls["train_tracker"] = interval_seconds

    monkeypatch.setattr(main_module, "start_simulator", fake_start_simulator)
    monkeypatch.setattr(main_module, "start_train_tracker", fake_start_train_tracker)
    monkeypatch.setattr(main_module, "start_retention_job", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "start_notification_bin_retention_job", lambda *a, **k: None)
    monkeypatch.setattr(main_module, "stop_simulator", AsyncMock())
    monkeypatch.setattr(main_module, "stop_train_tracker", AsyncMock())
    monkeypatch.setattr(main_module, "stop_retention_job", AsyncMock())
    monkeypatch.setattr(main_module, "stop_notification_bin_retention_job", AsyncMock())
    monkeypatch.setattr(main_module.manager, "bind_loop", lambda *a, **k: None)
    monkeypatch.setattr(main_module.manager, "start_relay", lambda: None)
    monkeypatch.setattr(main_module.manager, "start_reaper", lambda: None)
    monkeypatch.setattr(main_module.manager, "stop_relay", lambda: None)
    monkeypatch.setattr(main_module.manager, "stop_reaper", AsyncMock())
    monkeypatch.setattr(
        main_module.notification_dispatch_queue, "recover_pending_jobs", lambda: 0
    )

    async def _enter_and_exit_lifespan():
        async with main_module.lifespan(main_module.app):
            pass

    asyncio.run(_enter_and_exit_lifespan())

    assert calls["simulator"] == settings.SIMULATOR_INTERVAL_SECONDS == 60
    assert calls["train_tracker"] == settings.TRAIN_TRACK_INTERVAL_SECONDS == 60


# --- 9. Untouched cadences stay untouched --------------------------------

def test_crowd_history_interval_setting_remains_60_seconds():
    assert settings.CROWD_HISTORY_INTERVAL_SECONDS == 60


# --- 6. No stale 5s/10s default remains anywhere in the chain -----------

def test_no_5_or_10_second_default_remains_in_scheduler_or_run_forever():
    checks = {
        "scheduler.start_simulator": scheduler.start_simulator,
        "scheduler.start_train_tracker": scheduler.start_train_tracker,
        "csv_replay_simulator.run_forever": csv_replay_simulator.run_forever,
        "train_simulator.run_forever": train_simulator.run_forever,
    }
    for label, fn in checks.items():
        default = inspect.signature(fn).parameters["interval_seconds"].default
        assert default not in (5, 10), f"{label} still defaults interval_seconds to {default}"
        assert default == 60, f"{label} should default interval_seconds to 60, got {default}"
