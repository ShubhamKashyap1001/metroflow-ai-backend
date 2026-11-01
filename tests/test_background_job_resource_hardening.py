
import asyncio

import pytest

from app.core import notification_executor
from app.core.config import settings
from app.simulator import scheduler
from app.simulator.leader_election import LeaderElection
from app.websocket.manager import ConnectionManager

@pytest.fixture(autouse=True)
def _reset_scheduler_globals(monkeypatch):
    monkeypatch.setattr(scheduler, "_retention_election", None)
    monkeypatch.setattr(scheduler, "_notification_bin_retention_election", None)
    monkeypatch.setattr(scheduler, "_report_schedule_election", None)

class _FakeLeaderElection:

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

def test_start_report_scheduler_is_idempotent_once_already_started(fake_leader_election):
    scheduler.start_report_scheduler(object(), 60)
    first_election = scheduler._report_schedule_election
    scheduler.start_report_scheduler(object(), 60)
    assert scheduler._report_schedule_election is first_election

def test_calling_all_four_scheduler_starts_twice_creates_no_duplicates(monkeypatch, fake_leader_election):
    monkeypatch.setattr(scheduler, "_crowd_election", None)
    monkeypatch.setattr(scheduler, "_train_election", None)
    monkeypatch.setattr(scheduler, "_report_schedule_election", None)

    session_factory = object()
    scheduler.start_simulator(session_factory, 60)
    scheduler.start_train_tracker(session_factory, 60)
    scheduler.start_retention_job(session_factory, 3600)
    scheduler.start_notification_bin_retention_job(session_factory, 300)
    scheduler.start_report_scheduler(session_factory, 60)

    first = {
        "crowd": scheduler._crowd_election,
        "train": scheduler._train_election,
        "retention": scheduler._retention_election,
        "notif_bin": scheduler._notification_bin_retention_election,
        "reports": scheduler._report_schedule_election,
    }

    scheduler.start_simulator(session_factory, 60)
    scheduler.start_train_tracker(session_factory, 60)
    scheduler.start_retention_job(session_factory, 3600)
    scheduler.start_notification_bin_retention_job(session_factory, 300)
    scheduler.start_report_scheduler(session_factory, 60)

    assert scheduler._crowd_election is first["crowd"]
    assert scheduler._train_election is first["train"]
    assert scheduler._retention_election is first["retention"]
    assert scheduler._notification_bin_retention_election is first["notif_bin"]
    assert scheduler._report_schedule_election is first["reports"]

def test_manager_start_relay_and_reaper_are_idempotent_across_repeated_startup():

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

    async def scenario():
        manager = ConnectionManager()
        manager.bind_loop(asyncio.get_running_loop())

        async def _dead_on_arrival():
            return  

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

def test_election_tick_does_not_accumulate_worker_tasks_across_many_ticks(monkeypatch):
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

            assert tasks_after - tasks_before == 1, (
                f"expected exactly 1 new task after 10 ticks, "
                f"got {tasks_after - tasks_before}"
            )
            assert election.is_active()

            await election.stop()
            assert len(asyncio.all_tasks()) == tasks_before

    asyncio.run(scenario())

def test_election_stop_cancels_and_awaits_both_tasks_cleanly(monkeypatch):
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

            tasks_before = len(asyncio.all_tasks())
            await election._election_tick()
            tasks_after = len(asyncio.all_tasks())
            assert tasks_after - tasks_before == 1

            await election.stop()

    asyncio.run(scenario())

def test_notification_dispatch_worker_count_matches_configured_setting():
    assert notification_executor._executor._max_workers == settings.NOTIFICATION_DISPATCH_WORKERS

def test_notification_executor_shutdown_is_safe_to_call_more_than_once(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    test_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-notify")
    monkeypatch.setattr(notification_executor, "_executor", test_executor)

    notification_executor.shutdown(wait=True)
    notification_executor.shutdown(wait=True)  

    notification_executor.submit(lambda: None)
