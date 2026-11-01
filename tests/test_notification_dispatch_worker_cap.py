import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.core import notification_executor
from app.core.config import settings

class TestConfiguredDefault:
    def test_notification_dispatch_workers_default_is_two(self):
        assert settings.NOTIFICATION_DISPATCH_WORKERS == 2

    def test_executor_is_sized_from_settings(self):
        assert notification_executor._executor._max_workers == settings.NOTIFICATION_DISPATCH_WORKERS
        assert notification_executor._executor._max_workers == 2

class TestConcurrencyIsActuallyCapped:

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

        time.sleep(0.3)
        assert max_observed == 2, (
            f"expected at most 2 concurrent notification dispatch jobs, "
            f"observed {max_observed}"
        )

        release.set()
        test_executor.shutdown(wait=True)

class TestSubmissionStillWorks:

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

        notification_executor.submit(third_job)
        assert not third_ran.is_set(), "3rd job ran immediately despite both workers being busy"

        hold.set()
        assert third_ran.wait(timeout=2), "queued job never ran after a worker freed up"

        test_executor.shutdown(wait=True)

    def test_shutdown_remains_safe_to_call_more_than_once_at_the_lower_cap(self, monkeypatch):
        test_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-notify")
        monkeypatch.setattr(notification_executor, "_executor", test_executor)

        notification_executor.shutdown(wait=True)
        notification_executor.shutdown(wait=True)  

        notification_executor.submit(lambda: None)
