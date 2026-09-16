"""STEP 4 OOM fix: notification dispatch executor capped at 2 workers.

Focused regression coverage for lowering NOTIFICATION_DISPATCH_WORKERS
from 8 to 2 (see app/core/config.py / app/core/notification_executor.py).
Does not send any real email/SMS - it only proves the executor's
concurrency ceiling and that job submission/execution still works
through the real (in-process) ThreadPoolExecutor.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.core import notification_executor
from app.core.config import settings


class TestConfiguredDefault:
    def test_notification_dispatch_workers_default_is_two(self):
        """The production default for this setting is now 2 (was 8)."""
        assert settings.NOTIFICATION_DISPATCH_WORKERS == 2

    def test_executor_is_sized_from_settings(self):
        """The module-level singleton pool must be sized from
        settings.NOTIFICATION_DISPATCH_WORKERS, not a hardcoded value -
        same assertion style as
        test_background_job_resource_hardening.py's existing check,
        pinned here specifically for the new default."""
        assert notification_executor._executor._max_workers == settings.NOTIFICATION_DISPATCH_WORKERS
        assert notification_executor._executor._max_workers == 2


class TestConcurrencyIsActuallyCapped:
    """Prove - against a real ThreadPoolExecutor, not just the size
    attribute - that at most 2 submitted jobs ever run at the same
    time, and any further jobs wait in the executor's queue instead of
    running immediately."""

    def test_at_most_two_jobs_run_concurrently(self, monkeypatch):
        test_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-notify")
        monkeypatch.setattr(notification_executor, "_executor", test_executor)

        concurrent_count = 0
        max_observed = 0
        lock = threading.Lock()
        release = threading.Event()
        job_count = 6

        def slow_job():
            nonlocal concurrent_count, max_observed
            with lock:
                concurrent_count += 1
                max_observed = max(max_observed, concurrent_count)
            release.wait(timeout=2)
            with lock:
                concurrent_count -= 1

        for _ in range(job_count):
            notification_executor.submit(slow_job)

        # Give the pool a moment to pick up as many jobs as it can
        # concurrently run, then confirm it plateaued at 2 (not 6, not
        # 8) before releasing everything.
        time.sleep(0.3)
        assert max_observed == 2, (
            f"expected at most 2 concurrent notification dispatch jobs, "
            f"observed {max_observed}"
        )

        release.set()
        test_executor.shutdown(wait=True)


class TestSubmissionStillWorks:
    """Notification dispatch functionality itself (submit -> job
    actually runs) is unaffected by the lower worker cap."""

    def test_submitted_job_still_executes(self, monkeypatch):
        test_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-notify")
        monkeypatch.setattr(notification_executor, "_executor", test_executor)

        ran = threading.Event()

        def job():
            ran.set()

        notification_executor.submit(job)
        assert ran.wait(timeout=2), "submitted notification job did not run"

        test_executor.shutdown(wait=True)

    def test_jobs_beyond_capacity_queue_instead_of_being_dropped(self, monkeypatch):
        """With max_workers=2, a 3rd job submitted while both workers
        are busy must still eventually run (queued), not be silently
        discarded."""
        test_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-notify")
        monkeypatch.setattr(notification_executor, "_executor", test_executor)

        hold = threading.Event()
        started = threading.Event()
        third_ran = threading.Event()

        def blocker():
            started.set()
            hold.wait(timeout=2)

        def third_job():
            third_ran.set()

        notification_executor.submit(blocker)
        notification_executor.submit(blocker)
        assert started.wait(timeout=2)

        # Both workers are occupied - this 3rd job must queue, not run
        # immediately, and must not be dropped.
        notification_executor.submit(third_job)
        assert not third_ran.is_set(), "3rd job ran immediately despite both workers being busy"

        hold.set()
        assert third_ran.wait(timeout=2), "queued job never ran after a worker freed up"

        test_executor.shutdown(wait=True)

    def test_shutdown_remains_safe_to_call_more_than_once_at_the_lower_cap(self, monkeypatch):
        test_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-notify")
        monkeypatch.setattr(notification_executor, "_executor", test_executor)

        notification_executor.shutdown(wait=True)
        notification_executor.shutdown(wait=True)  # must not raise

        # Submitting after shutdown is logged, not raised, back to the caller.
        notification_executor.submit(lambda: None)
