"""Regression test for the WebSocket connection/reconnect/event-failure
counters (app/websocket/manager.py) and the simulator leader/heartbeat
counters (app/simulator/leader_election.py).

Same rationale and technique as tests/test_websocket_relay_reconnect.py:
`app/websocket/manager.py` and `app/simulator/leader_election.py` both
depend on `fastapi`/`redis` (neither installable in this offline
sandbox), so this runs the already-verified, dependency-free harness
at `scripts/verify_realtime_metrics.py` as a subprocess and asserts it
exits 0. That script imports the REAL manager.py/leader_election.py
modules unmodified (with fake `fastapi`/`app.core.cache` stand-ins
injected), so this is a regression test against the actual shipped
counter logic - including an explicit connect -> disconnect ->
reconnect cycle - not a reimplementation of it.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_realtime_metrics.py"


def test_realtime_metrics_verification_script_passes():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"scripts/verify_realtime_metrics.py failed (exit {result.returncode}).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "All 29 checks PASSED" in result.stdout, result.stdout
