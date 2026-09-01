"""Regression test for the WebSocket cross-process relay's
auto-reconnect behavior. See docs/realtime-websocket-system.md.

`app/websocket/manager.py`'s relay depends on `fastapi` (for the
`WebSocket` type hint) and `app.core.cache` (which imports the
third-party `redis` package) - neither installable in this offline
sandbox (see docs/realtime-websocket-system.md / docs/background-jobs-and-leader-election.md for the same,
recurring constraint). Rather than duplicate the fake-module-injection
technique in two places, this test runs the already-verified,
dependency-free harness at `scripts/verify_ws_relay_reconnect.py` - the
same technique this project already uses for `scripts/verify_ws_manager.py`
and `scripts/verify_leader_election.py` - as a
subprocess, and asserts it exits 0 (all checks passed). That script
imports the REAL `app/websocket/manager.py` module unmodified (with
fake `fastapi`/`app.core.cache` stand-ins injected), so this is a
regression test against the actual shipped relay code, not a
reimplementation of it.

In this project's normal (networked) dev/CI environment, where
`redis`/`fastapi` from requirements.txt are installed, `manager.py`
can also be imported directly and driven with a real event loop the
same way `scripts/verify_ws_relay_reconnect.py` already does - this
test just doesn't require that to already prove the fix works.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "verify_ws_relay_reconnect.py"


def test_ws_relay_reconnect_verification_script_passes():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"scripts/verify_ws_relay_reconnect.py failed (exit {result.returncode}).\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "All 13 checks PASSED" in result.stdout, result.stdout
