import asyncio
from unittest.mock import patch

import pytest

from app.simulator import local_lock
from app.simulator.leader_election import LeaderElection

async def _idle_loop():
    await asyncio.Event().wait()

class _FakeClock:

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

@pytest.fixture(autouse=True)
def _cleanup_locks():
    yield
    for name in (
        "outage_test_immediate",
        "outage_test_fallback",
        "outage_test_resume",
        "outage_test_never_reachable",
        "outage_test_disabled_unaffected",
    ):
        local_lock.release(name)

def _run(coro):
    return asyncio.run(coro)

def test_steps_down_immediately_when_outage_just_started(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr("app.simulator.leader_election.time.monotonic", clock)

    async def scenario():
        election = LeaderElection("outage_test_immediate", _idle_loop, lease_seconds=15)
        with patch("app.simulator.leader_election.cache.redis_status",
                   return_value={"connected": True, "state": "connected"}):
            await election._election_tick()  

        with patch("app.simulator.leader_election.cache.redis_status",
                   return_value={"connected": False, "state": "unreachable"}), \
             patch("app.simulator.leader_election.cache.try_acquire_or_renew_lock",
                   return_value=False):
            await election._election_tick()  
            assert election.is_active() is False, (
                "must NOT fall back to the local lock this early - a "
                "pre-outage lease could still legitimately be held elsewhere"
            )

        await election.stop()

    _run(scenario())

def test_falls_back_to_local_lock_after_a_full_lease_window(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr("app.simulator.leader_election.time.monotonic", clock)

    async def scenario():
        election = LeaderElection("outage_test_fallback", _idle_loop, lease_seconds=15)
        with patch("app.simulator.leader_election.cache.redis_status",
                   return_value={"connected": True, "state": "connected"}):
            await election._election_tick()

        with patch("app.simulator.leader_election.cache.redis_status",
                   return_value={"connected": False, "state": "unreachable"}), \
             patch("app.simulator.leader_election.cache.try_acquire_or_renew_lock",
                   return_value=False):
            await election._election_tick()  
            assert election.is_active() is False

            clock.advance(15)  
            await election._election_tick()
            assert election.is_active() is True, (
                "after a full lease window with no way to renew/verify "
                "anything, the simulator must resume via the local-lock "
                "fallback rather than stay stopped forever"
            )

        await election.stop()

    _run(scenario())

def test_resumes_redis_path_and_drops_local_lock_on_reconnect(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr("app.simulator.leader_election.time.monotonic", clock)

    async def scenario():
        election = LeaderElection("outage_test_resume", _idle_loop, lease_seconds=15)
        with patch("app.simulator.leader_election.cache.redis_status",
                   return_value={"connected": True, "state": "connected"}):
            await election._election_tick()

        with patch("app.simulator.leader_election.cache.redis_status",
                   return_value={"connected": False, "state": "unreachable"}), \
             patch("app.simulator.leader_election.cache.try_acquire_or_renew_lock",
                   return_value=False):
            clock.advance(15)
            await election._election_tick()
            assert election.is_active() is True
            assert local_lock.is_held("outage_test_resume") is True

        with patch("app.simulator.leader_election.cache.redis_status",
                   return_value={"connected": True, "state": "connected"}), \
             patch("app.simulator.leader_election.cache.try_acquire_or_renew_lock",
                   return_value=True) as mock_acquire:
            await election._election_tick()
            assert local_lock.is_held("outage_test_resume") is False, (
                "the local-lock fallback must be released once Redis is "
                "trustworthy again"
            )
            assert election.is_active() is True
            mock_acquire.assert_called_once()

        await election.stop()

    _run(scenario())

def test_never_reachable_still_falls_back_immediately(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr("app.simulator.leader_election.time.monotonic", clock)

    async def scenario():
        election = LeaderElection("outage_test_never_reachable", _idle_loop, lease_seconds=15)
        with patch("app.simulator.leader_election.cache.redis_status",
                   return_value={"connected": False, "state": "unreachable"}):
            await election._election_tick()
            assert election.is_active() is True

        await election.stop()

    _run(scenario())

def test_disabled_state_is_unaffected_by_outage_tracking(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr("app.simulator.leader_election.time.monotonic", clock)

    async def scenario():
        election = LeaderElection("outage_test_disabled_unaffected", _idle_loop, lease_seconds=15)
        with patch("app.simulator.leader_election.cache.redis_status",
                   return_value={"connected": False, "state": "disabled"}):
            await election._election_tick()
            assert election.is_active() is True

        await election.stop()

    _run(scenario())
