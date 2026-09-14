"""Render Free 512MB fix: prevent duplicate/unbounded background jobs.

This file adds the regression coverage that was missing around the
existing background-job lifecycle machinery
(`app/simulator/leader_election.py`, `app/simulator/scheduler.py`,
`app/websocket/manager.py`'s reaper, `app/core/notification_executor.py`).
Almost all of the required guarantees (idempotent start, bounded
crash-restart via the election poll interval, no `create_task` inside
any loop body, a single bounded notification worker pool) were already
in place from prior work - see docs/background-jobs-and-leader-election.md.
The one real gap this file's fix addresses is `ConnectionManager.
start_reaper()`, which used to only check "does a task object exist"
instead of "is it still running", unlike every other loop's start()
guard - see `test_reaper_restarts_after_a_crash_instead_of_staying_dead`
below for the direct regression test.

None of these tests spin up real Redis/Postgres - they exercise the
same in-process objects `app/main.py`'s lifespan wires together,
using the same fakes/monkeypatch style as
tests/test_simulator_scheduler_intervals.py and
tests/test_leader_election.py.
"""

import asyncio

import pytest

from app.core import notification_executor
from app.core.config import settings
from app.simulator import scheduler
from app.simulator.leader_election import LeaderElection
from app.websocket.manager import ConnectionManager


# ---------------------------------------------------------------------
# 1. Idempotent start for the two jobs test_simulator_scheduler_
#    intervals.py doesn't already cover (crowd retention / notification
#    bin retention) - mirrors that file's pattern exactly for the
#    simulator/tracker.
# ---------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_scheduler_globals(monkeypatch):
    monkeypatch.setattr(scheduler, "_retention_election", None)
    monkeypatch.setattr(scheduler, "_notification_bin_retention_election", None)


class _FakeLeaderElection:
    """Records the (name, run_loop) it was constructed with instead of
    actually starting a background asyncio task - see
    test_simulator_scheduler_intervals.py's identical fixture."""

    def __init__(self, name, run_loop, *, lease_seconds=15, poll_seconds=3):
        self.name = name
        self.run_loop = run_loop
        self.start_calls = 0

    def start(self):
        self.start_calls += 1


@pytest.fixture
def fake_leader_election(monkeypatch):
    monkeypatch.setattr(scheduler, "LeaderElection", _FakeLeaderElection)
    return _FakeLeaderElection


def test_start_retention_job_is_idempotent_once_already_started(fake_leader_election):
    scheduler.start_retention_job(object(), 3600)
    first_election = scheduler._retention_election

    scheduler.start_retention_job(object(), 3600)

    assert scheduler._retention_election is first_election
    assert first_election.start_calls == 2, (
        "start() itself is called again (that's fine - LeaderElection.start() "
        "is its own idempotent no-op) but no SECOND election/loop object "
        "should ever be constructed"
    )


def test_start_notification_bin_retention_job_is_idempotent_once_already_started(fake_leader_election):
    scheduler.start_notification_bin_retention_job(object(), 300)
    first_election = scheduler._notification_bin_retention_election

    scheduler.start_notification_bin_retention_job(object(), 300)

    assert scheduler._notification_bin_retention_election is first_election


# ---------------------------------------------------------------------
# 2. Calling the full startup sequence twice (all four scheduler jobs +
#    the WebSocket relay/reaper) never creates a duplicate of anything.
#    This is the direct test for requirement "calling startup twice
#    does not create duplicate workers", exercised across every job
#    app/main.py's lifespan starts, not just one of them in isolation.
# ---------------------------------------------------------------------

def test_calling_all_four_scheduler_starts_twice_creates_no_duplicates(monkeypatch, fake_leader_election):
    monkeypatch.setattr(scheduler, "_crowd_election", None)
    monkeypatch.setattr(scheduler, "_train_election", None)

    session_factory = object()
    scheduler.start_simulator(session_factory, 60)
    scheduler.start_train_tracker(session_factory, 60)
    scheduler.start_retention_job(session_factory, 3600)
    scheduler.start_notification_bin_retention_job(session_factory, 300)

    first = {
        "crowd": scheduler._crowd_election,
        "train": scheduler._train_election,
        "retention": scheduler._retention_election,
        "notif_bin": scheduler._notification_bin_retention_election,
    }

    # Simulate a second startup pass (e.g. a duplicated startup signal)
    # without an intervening shutdown.
    scheduler.start_simulator(session_factory, 60)
    scheduler.start_train_tracker(session_factory, 60)
    scheduler.start_retention_job(session_factory, 3600)
    scheduler.start_notification_bin_retention_job(session_factory, 300)

    assert scheduler._crowd_election is first["crowd"]
    assert scheduler._train_election is first["train"]
    assert scheduler._retention_election is first["retention"]
    assert scheduler._notification_bin_retention_election is first["notif_bin"]


def test_manager_start_relay_and_reaper_are_idempotent_across_repeated_startup():
    """app/main.py's lifespan calls manager.start_relay()/start_reaper()
    on every startup. Both must be no-ops the second time while the
    first instance is still alive/running."""

    async def scenario():
        manager = ConnectionManager()
        manager.bind_loop(asyncio.get_running_loop())

        manager.start_relay()
        first_thread = manager._relay_thread
        manager.start_relay()
        assert manager._relay_thread is first_thread, (
            "a second start_relay() call while the relay thread is still "
            "alive must not spawn a second thread"
        )

        manager.start_reaper()
        first_reaper_task = manager._reaper_task
        manager.start_reaper()
        assert manager._reaper_task is first_reaper_task, (
            "a second start_reaper() call while the reaper task is still "
            "running must not spawn a second task"
        )

        manager.stop_relay()
        await manager.stop_reaper()

    asyncio.run(scenario())


def test_reaper_restarts_after_a_crash_instead_of_staying_dead():
    """Regression test for the fix in this pass: start_reaper() used to
    check only `self._reaper_task is not None`, which - unlike every
    other loop's start() guard in this codebase (LeaderElection.start(),
    LeaderElection._start_worker()) - never checked whether that task
    had actually finished. A crashed reaper task would therefore be
    silently treated as "still running" forever, and a later
    start_reaper() call would never replace it. With the fix, a done
    (crashed or otherwise finished) task is correctly treated the same
    as "not started" and replaced."""

    async def scenario():
        manager = ConnectionManager()
        manager.bind_loop(asyncio.get_running_loop())

        async def _dead_on_arrival():
            return  # finishes immediately - simulates a crashed/exited task

        manager._reaper_task = asyncio.ensure_future(_dead_on_arrival())
        await manager._reaper_task
        assert manager._reaper_task.done()

        manager.start_reaper()

        assert manager._reaper_task is not None
        assert not manager._reaper_task.done(), (
            "start_reaper() must replace an already-finished task with a "
            "fresh one instead of treating the stale reference as proof "
            "the loop is still active"
        )

        await manager.stop_reaper()

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# 3. Recurring loops do not accumulate asyncio tasks across many
#    election ticks, and cancellation/shutdown terminates cleanly.
# ---------------------------------------------------------------------

def test_election_tick_does_not_accumulate_worker_tasks_across_many_ticks(monkeypatch):
    """Repeatedly calling _election_tick() (as the real _election_loop
    does every poll_seconds) while this process keeps winning must
    never leave more than the one active worker task alive - no
    per-tick create_task() pile-up."""
    from unittest.mock import patch

    async def fake_loop():
        await asyncio.Event().wait()

    async def scenario():
        with patch(
            "app.simulator.leader_election.cache.redis_status",
            return_value={"connected": False, "state": "disabled"},
        ):
            election = LeaderElection("tick_accumulation_test", fake_loop)
            tasks_before = len(asyncio.all_tasks())

            for _ in range(10):
                await election._election_tick()

            tasks_after = len(asyncio.all_tasks())
            # Exactly one new task (the worker) should exist, regardless
            # of how many ticks ran.
            assert tasks_after - tasks_before == 1, (
                f"expected exactly 1 new task after 10 ticks, "
                f"got {tasks_after - tasks_before}"
            )
            assert election.is_active()

            await election.stop()
            assert len(asyncio.all_tasks()) == tasks_before

    asyncio.run(scenario())


def test_election_stop_cancels_and_awaits_both_tasks_cleanly(monkeypatch):
    """stop() must leave neither the election-loop task nor the worker
    task pending - both cancelled and awaited, so shutdown never
    abandons a task."""
    from unittest.mock import patch

    async def fake_loop():
        await asyncio.Event().wait()

    async def scenario():
        with patch(
            "app.simulator.leader_election.cache.redis_status",
            return_value={"connected": False, "state": "disabled"},
        ):
            election = LeaderElection("stop_cleanliness_test", fake_loop)
            election.start()
            # Let the election loop run at least one real tick.
            await election._election_tick()
            assert election.is_active()

            worker_task = election._worker_task
            election_task = election._election_task

            await election.stop()

            assert worker_task.done() and worker_task.cancelled()
            assert election_task.done() and election_task.cancelled()
            assert election._worker_task is None
            assert election._election_task is None

    asyncio.run(scenario())


def test_worker_crash_does_not_respawn_until_the_next_election_tick(monkeypatch):
    """A background-job exception must not create an unbounded restart
    loop or rapidly spawn replacement tasks: if the worker coroutine
    raises, _on_worker_done() only records the crash - it must NOT
    itself call _start_worker() again. The loop only comes back on the
    NEXT explicit _election_tick() (which in production is throttled to
    once every poll_seconds), never immediately/automatically."""
    from unittest.mock import patch

    async def crashing_loop():
        raise RuntimeError("simulated worker crash")

    async def scenario():
        with patch(
            "app.simulator.leader_election.cache.redis_status",
            return_value={"connected": False, "state": "disabled"},
        ):
            election = LeaderElection("bounded_restart_test", crashing_loop)
            await election._election_tick()

            # Await the worker task directly so _on_worker_done() (added
            # via add_done_callback at task-creation time, before this
            # await's own internal completion callback) is guaranteed to
            # have already run by the time we inspect its effects below.
            worker_task = election._worker_task
            assert worker_task is not None
            with pytest.raises(RuntimeError):
                await worker_task

            assert worker_task.done()
            assert not election.is_active(), (
                "a crashed worker must not be silently treated as active, "
                "and nothing should have auto-restarted it yet"
            )
            assert election._last_worker_error is not None

            # Only an explicit next tick brings it back - and it comes
            # back exactly once, not as a burst of replacement tasks.
            tasks_before = len(asyncio.all_tasks())
            await election._election_tick()
            tasks_after = len(asyncio.all_tasks())
            assert tasks_after - tasks_before == 1

            await election.stop()

    asyncio.run(scenario())


# ---------------------------------------------------------------------
# 4. Notification dispatch worker pool: count unchanged, shutdown is
#    safe to call more than once.
# ---------------------------------------------------------------------

def test_notification_dispatch_worker_count_matches_configured_setting():
    """Fix must not increase NOTIFICATION_DISPATCH_WORKERS - the pool is
    a module-level singleton sized from settings at import time."""
    assert notification_executor._executor._max_workers == settings.NOTIFICATION_DISPATCH_WORKERS


def test_notification_executor_shutdown_is_safe_to_call_more_than_once(monkeypatch):
    """app/main.py's lifespan calls notification_executor.shutdown() on
    every shutdown, and atexit registers it too - both could fire in
    the same process. ThreadPoolExecutor.shutdown() is documented as
    idempotent; this pins that behavior for this module specifically so
    a future change can't quietly break it."""
    from concurrent.futures import ThreadPoolExecutor

    test_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-notify")
    monkeypatch.setattr(notification_executor, "_executor", test_executor)

    notification_executor.shutdown(wait=True)
    notification_executor.shutdown(wait=True)  # must not raise

    # Submitting after shutdown is logged, not raised, back to the caller.
    notification_executor.submit(lambda: None)
