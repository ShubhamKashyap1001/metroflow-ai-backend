
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
