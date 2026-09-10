import asyncio
from unittest.mock import patch

import pytest

from app.simulator import local_lock
from app.simulator.leader_election import LeaderElection


async def _idle_loop():
    await asyncio.Event().wait()


class _FakeClock:
    """Lets a test move `time.monotonic()` forward deterministically,
    patched in as `app.simulator.leader_election.time.monotonic`."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _cleanup_locks():
    """Belt-and-suspenders: release any lock a test forgot to, so a
    failure in one test can't leave a stale flock affecting the next."""
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
    """A lease acquired moments before the outage could still be
    legitimately valid elsewhere - falling back to the local lock this
    early would risk a genuine split-brain, so this must still fail
    CLOSED (step down), unchanged from the pre-fix behaviour."""
    clock = _FakeClock()
    monkeypatch.setattr("app.simulator.leader_election.time.monotonic", clock)

    async def scenario():
        election = LeaderElection("outage_test_immediate", _idle_loop, lease_seconds=15)
        with patch("app.simulator.leader_election.cache.redis_status",
                   return_value={"connected": True, "state": "connected"}):
            await election._election_tick()  # Redis is up: nothing special yet.

        with patch("app.simulator.leader_election.cache.redis_status",
                   return_value={"connected": False, "state": "unreachable"}), \
             patch("app.simulator.leader_election.cache.try_acquire_or_renew_lock",
                   return_value=False):
            await election._election_tick()  # outage just started (elapsed = 0s)
            assert election.is_active() is False, (
                "must NOT fall back to the local lock this early - a "
                "pre-outage lease could still legitimately be held elsewhere"
            )

        await election.stop()

    _run(scenario())


def test_falls_back_to_local_lock_after_a_full_lease_window(monkeypatch):
    """Once the outage has lasted a full lease window, no lease from
    before the outage can possibly still be valid anywhere - it's now
    safe (and necessary for availability) to fall back to the local
    lock instead of staying stepped-down forever."""
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
            await election._election_tick()  # t+0s: still failing closed
            assert election.is_active() is False

            clock.advance(15)  # t+15s: a full lease window has now elapsed
            await election._election_tick()
            assert election.is_active() is True, (
                "after a full lease window with no way to renew/verify "
                "anything, the simulator must resume via the local-lock "
                "fallback rather than stay stopped forever"
            )

        await election.stop()

    _run(scenario())


def test_resumes_redis_path_and_drops_local_lock_on_reconnect(monkeypatch):
    """Once Redis comes back, the process must go back to trusting the
    Redis lease as the sole source of truth and release the local
    lock it was leaning on during the outage - unchanged contract from
    the "never reachable" fallback."""
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
    """Regression guard: a process that has NEVER seen Redis connected
    (the original Phase 7A case) must still fall back immediately -
    the new lease-window gate only applies to a real, mid-flight
    outage after a genuine prior connection."""
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
    """`CACHE_ENABLED=False` must keep behaving exactly as before -
    immediate local-lock fallback, no lease-window wait, regardless of
    whether this process ever saw Redis connected."""
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
