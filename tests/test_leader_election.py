
import os
import signal
import subprocess
import sys
import tempfile
import time

import pytest

from app.simulator import local_lock

# Children are spawned as bare `python child.py` subprocesses, which
# do NOT inherit pytest's sys.path - they need the repo root on
# PYTHONPATH themselves to `import app.simulator.local_lock`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    return env

# ---------------------------------------------------------------------
# Real multi-process tests (no mocking - actual OS processes, actual
# kernel-enforced flock contention).
# ---------------------------------------------------------------------

_CHILD_SCRIPT = """
import os, sys, time
os.environ["METROFLOW_LOCK_DIR"] = sys.argv[4]
from app.simulator import local_lock
name, result_path, mode = sys.argv[1], sys.argv[2], sys.argv[3]
won = local_lock.try_acquire(name)
with open(result_path, "w") as f:
    f.write("WON" if won else "LOST")
if won:
    if mode == "hold":
        time.sleep(30)
    elif mode == "hold_brief":
        time.sleep(1.2)
        local_lock.release(name)
    elif mode == "release":
        local_lock.release(name)
        time.sleep(1)
"""


class TestLocalLockRealMultiProcess:
    @pytest.fixture
    def lock_dir(self, tmp_path):
        d = tmp_path / "locks"
        d.mkdir()
        return str(d)

    @pytest.fixture
    def child_script(self, tmp_path):
        path = tmp_path / "_child_try_acquire.py"
        path.write_text(_CHILD_SCRIPT)
        return str(path)

    def _spawn(self, child_script, name, result_file, mode, lock_dir):
        return subprocess.Popen(
            [sys.executable, child_script, name, str(result_file), mode, lock_dir],
            env=_child_env(),
        )

    def test_single_process_wins_its_own_lock(self, child_script, lock_dir, tmp_path):
        rf = tmp_path / "r0.txt"
        p = self._spawn(child_script, "solo_sim", rf, "release", lock_dir)
        assert p.wait(timeout=10) == 0
        assert rf.read_text().strip() == "WON"

    def test_eight_concurrent_workers_exactly_one_leader(self, child_script, lock_dir, tmp_path):
        """Reproduces `uvicorn --workers 8` starting simultaneously with
        Redis unreachable: exactly one process must win the lock -
        this is the direct regression test for bug #2."""
        n = 8
        procs, result_files = [], []
        for i in range(n):
            rf = tmp_path / f"race_{i}.txt"
            result_files.append(rf)
            procs.append(self._spawn(child_script, "crowd_simulator", rf, "hold_brief", lock_dir))
        for p in procs:
            assert p.wait(timeout=10) == 0

        results = [rf.read_text().strip() for rf in result_files]
        assert results.count("WON") == 1, f"expected exactly 1 leader among {n}, got {results}"
        assert results.count("LOST") == n - 1

    def test_lock_is_reacquirable_after_clean_release(self, child_script, lock_dir, tmp_path):
        rf1 = tmp_path / "seq1.txt"
        p1 = self._spawn(child_script, "sequential_sim", rf1, "release", lock_dir)
        p1.wait(timeout=10)
        assert rf1.read_text().strip() == "WON"

        rf2 = tmp_path / "seq2.txt"
        p2 = self._spawn(child_script, "sequential_sim", rf2, "release", lock_dir)
        p2.wait(timeout=10)
        assert rf2.read_text().strip() == "WON"

    def test_killed_holder_releases_lock_for_failover(self, child_script, lock_dir, tmp_path):
        """SIGKILL (hard crash, no graceful shutdown) must still free
        the lock immediately via OS-level auto-release - no manual
        cleanup or TTL wait required, unlike a naive PID-file scheme."""
        rf_holder = tmp_path / "crash_holder.txt"
        holder = self._spawn(child_script, "train_tracker", rf_holder, "hold", lock_dir)
        time.sleep(1.0)
        assert rf_holder.read_text().strip() == "WON"

        holder.send_signal(signal.SIGKILL)
        holder.wait(timeout=5)

        rf_next = tmp_path / "crash_next.txt"
        nxt = self._spawn(child_script, "train_tracker", rf_next, "release", lock_dir)
        assert nxt.wait(timeout=10) == 0
        assert rf_next.read_text().strip() == "WON", "standby should acquire immediately after crash"

    def test_fails_closed_when_flock_unavailable(self, monkeypatch, lock_dir):
        """If file locking itself can't be verified (e.g. an
        unsupported platform), try_acquire() must return False - never
        assume exclusivity it can't prove."""
        monkeypatch.setenv("METROFLOW_LOCK_DIR", lock_dir)
        monkeypatch.setattr(local_lock, "_FLOCK_AVAILABLE", False)
        assert local_lock.try_acquire("no_flock_here") is False

    def test_idempotent_reacquire_by_same_process(self, monkeypatch, lock_dir):
        monkeypatch.setenv("METROFLOW_LOCK_DIR", lock_dir)
        assert local_lock.try_acquire("idempotent_test") is True
        assert local_lock.try_acquire("idempotent_test") is True  # already held - no error
        local_lock.release("idempotent_test")

    def test_holder_id_scoped_reacquire_is_idempotent(self, monkeypatch, lock_dir):
        """A caller that identifies itself (holder_id) polling
        repeatedly for the SAME name must keep winning without ever
        touching the filesystem again - the fast path this module
        exists to provide for LeaderElection's every-few-seconds
        polling."""
        monkeypatch.setenv("METROFLOW_LOCK_DIR", lock_dir)
        assert local_lock.try_acquire("holder_scoped_test", "holder-A") is True
        assert local_lock.try_acquire("holder_scoped_test", "holder-A") is True
        local_lock.release("holder_scoped_test", "holder-A")

    def test_differently_identified_caller_in_same_process_does_not_win(self, monkeypatch, lock_dir):
        """BUGFIX regression: a second, differently-identified caller
        bidding for a name already held by a DIFFERENT identified
        caller in this same process must fail - not silently inherit
        the first caller's success via the module-level cache. Direct
        unit-level version of
        TestLeaderElectionIntegration::test_second_process_in_same_name_stands_by
        below, isolated to local_lock alone (no LeaderElection/cache
        mocking involved)."""
        monkeypatch.setenv("METROFLOW_LOCK_DIR", lock_dir)
        assert local_lock.try_acquire("contested_name", "holder-A") is True
        assert local_lock.try_acquire("contested_name", "holder-B") is False
        # holder-A still genuinely holds it throughout.
        assert local_lock.is_held("contested_name", "holder-A") is True
        assert local_lock.is_held("contested_name", "holder-B") is False
        local_lock.release("contested_name", "holder-A")
        # Once released, a different holder can now win it for real.
        assert local_lock.try_acquire("contested_name", "holder-B") is True
        local_lock.release("contested_name", "holder-B")

    def test_release_only_succeeds_for_the_matching_holder(self, monkeypatch, lock_dir):
        """A caller that never actually won the lock must not be able
        to release someone else's held lock out from under them."""
        monkeypatch.setenv("METROFLOW_LOCK_DIR", lock_dir)
        assert local_lock.try_acquire("release_scope_test", "holder-A") is True
        local_lock.release("release_scope_test", "holder-B")  # not the real holder - no-op
        assert local_lock.is_held("release_scope_test", "holder-A") is True
        local_lock.release("release_scope_test", "holder-A")  # real holder - actually releases
        assert local_lock.is_held("release_scope_test") is False


# ---------------------------------------------------------------------
# LeaderElection integration (mocked cache - needs `redis`/`fastapi`
# installed; see module docstring for why this wasn't executed in the
# offline verification sandbox).
# ---------------------------------------------------------------------

class TestLeaderElectionIntegration:
    # Plain `asyncio.run()` wrappers rather than `@pytest.mark.asyncio` -
    # this project's requirements.txt doesn't include the
    # `pytest-asyncio` plugin, so these stay runnable with plain
    # pytest (same reasoning as the rest of the suite).

    def test_falls_back_to_local_lock_when_redis_disabled(self, monkeypatch):
        import asyncio
        from unittest.mock import patch
        from app.simulator.leader_election import LeaderElection

        async def fake_loop():
            await asyncio.Event().wait()

        async def scenario():
            with patch("app.simulator.leader_election.cache.redis_status",
                       return_value={"connected": False, "state": "disabled"}):
                election = LeaderElection("test_loop_disabled", fake_loop)
                await election._election_tick()
                assert election.is_active() is True
                await election.stop()

        asyncio.run(scenario())

    def test_second_process_in_same_name_stands_by(self, monkeypatch):
        """Two LeaderElection instances bidding for the SAME name with
        Redis disabled must not both become active - proves bug #2 is
        closed even without spawning real processes, by exercising the
        exact same local_lock.try_acquire() call path twice."""
        import asyncio
        from unittest.mock import patch
        from app.simulator.leader_election import LeaderElection

        async def fake_loop():
            await asyncio.Event().wait()

        async def scenario():
            with patch("app.simulator.leader_election.cache.redis_status",
                       return_value={"connected": False, "state": "disabled"}):
                e1 = LeaderElection("shared_name_test", fake_loop)
                e2 = LeaderElection("shared_name_test", fake_loop)
                await e1._election_tick()
                await e2._election_tick()
                assert e1.is_active() != e2.is_active(), "exactly one of the two should be active"
                assert e1.is_active() or e2.is_active()
                await e1.stop()
                await e2.stop()

        asyncio.run(scenario())

    def test_second_process_stands_by_during_mid_flight_outage_fallback(self, monkeypatch):
        """Same guarantee as above, but for the Phase 7B path: Redis
        was reachable, then goes down mid-flight and stays down past
        a full lease window, so both instances fall back to the local
        lock (not the "disabled since startup" branch). Exactly one
        must still win."""
        import asyncio
        from unittest.mock import patch
        from app.simulator.leader_election import LeaderElection

        async def fake_loop():
            await asyncio.Event().wait()

        async def scenario():
            with patch("app.simulator.leader_election.cache.redis_status",
                       return_value={"connected": False, "state": "unreachable"}):
                e1 = LeaderElection("outage_fallback_test", fake_loop, lease_seconds=15)
                e2 = LeaderElection("outage_fallback_test", fake_loop, lease_seconds=15)
                # Simulate "Redis was reachable before, and the outage
                # has already exceeded a full lease window" directly,
                # rather than sleeping 15s in a test.
                e1._redis_ever_reachable = True
                e1._redis_down_since = 0.0
                e2._redis_ever_reachable = True
                e2._redis_down_since = 0.0

                await e1._election_tick()
                await e2._election_tick()
                assert e1.is_active() != e2.is_active(), "exactly one of the two should be active"
                await e1.stop()
                await e2.stop()

        asyncio.run(scenario())
