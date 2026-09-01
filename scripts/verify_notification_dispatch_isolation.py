"""Phase 8 verification (see docs/notification-delivery.md): proves that a
burst of concurrent alert email/SMS dispatches no longer delays
unrelated API requests.

Unlike scripts/verify_db_pool_pressure.py's Phase 6 harness (which had
to simulate SQLAlchemy's pool with a stdlib BoundedSemaphore, because
that sandbox had no network access to install anything), this one runs
the REAL app: real FastAPI routes, real AnyIO worker thread pool, real
`app/core/notification_executor.py`, real SQLAlchemy session/pool
lifecycle, over a real HTTP connection to a real `uvicorn.Server`
running in a background thread of this same process. The only thing
stubbed out is the actual SMTP/Twilio network call inside
`send_alert_emails`/`send_alert_sms` (replaced with a plain
`time.sleep()`) - there is no SMTP/Twilio account to send real
messages through in a sandbox/CI run, and doing so wouldn't change
what this is actually verifying (thread/connection contention, not
mail delivery).

PREREQUISITES
--------------
- A reachable Postgres the app can create tables in, referenced by
  DATABASE_URL (same requirement as running the app or pytest suite
  normally - see README.md).
- AUTH_DISABLED=True AND DEBUG=True in the environment/`.env` this
  script loads, so a request can authenticate as `Bearer <email>`
  without a real Supabase token (this is the same dev-only bypass
  `app/core/security.py` already supports - see its module docstring -
  not something this script adds). Both must be set: the bypass is
  gated on `settings.dev_auth_bypass_enabled` (AUTH_DISABLED AND
  DEBUG), so it stays off with AUTH_DISABLED alone in an environment
  where DEBUG defaults to False (e.g. production).
- The DB has at least one Station and a handful of active UserProfile
  rows with an email/phone set, plus one UserProfile with role=ADMIN
  or OPERATOR to raise alerts as. Point SEED_* env vars below at real
  rows, or run this against a dev DB seeded via
  `app/database/seed.py`/`seed_real_data.py`.

WHAT IT MEASURES
-----------------
1. Baseline: a few GET /alerts calls with nothing else happening.
2. Burst: N_ALERTS concurrent POST /alerts (each enqueues a stubbed-
   slow email+SMS dispatch) fired alongside N_READS concurrent GET
   /alerts, all at once.
3. Compares GET /alerts latency (and status codes) under the burst
   against the quiet baseline.

Run: `python3 scripts/verify_notification_dispatch_isolation.py`
(from the backend project root, with the venv/deps active).
"""
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAKE_NOTIFY_SLEEP = float(os.environ.get("FAKE_NOTIFY_SLEEP", "1.5"))
PORT = int(os.environ.get("VERIFY_PORT", "8199"))
N_ALERTS = int(os.environ.get("N_ALERTS", "45"))
N_READS = int(os.environ.get("N_READS", "15"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "40"))

SEED_STATION_ID = int(os.environ.get("SEED_STATION_ID", "1"))
SEED_ADMIN_EMAIL = os.environ.get("SEED_ADMIN_EMAIL", "admin@test.com")
SEED_READER_EMAIL = os.environ.get("SEED_READER_EMAIL", "user0@test.com")


def _stub_slow_transport():
    """Replace the two blocking network calls with a plain sleep, at
    the exact seam app/services/alert_service.py calls them through -
    see that module's `_dispatch()`."""
    import app.services.alert_service as alert_service

    def fake_send_alert_emails(recipients, **kwargs):
        time.sleep(FAKE_NOTIFY_SLEEP)
        return {r: "sent" for r in recipients}

    def fake_send_alert_sms(recipients, **kwargs):
        time.sleep(FAKE_NOTIFY_SLEEP)
        return {r: "sent" for r in recipients}

    alert_service.send_alert_emails = fake_send_alert_emails
    alert_service.send_alert_sms = fake_send_alert_sms


def _run_server():
    import uvicorn

    from app.main import app

    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def _wait_until_up(base_url: str, timeout: float = 20.0) -> None:
    import requests

    deadline = time.monotonic() + timeout
    last_exc = None
    while time.monotonic() < deadline:
        try:
            requests.get(f"{base_url}/healthz", timeout=2)
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(0.3)
    raise RuntimeError(f"Server did not come up in time: {last_exc}")


def main() -> int:
    import requests

    _stub_slow_transport()
    server, thread = _run_server()
    base = f"http://127.0.0.1:{PORT}/api/v1"
    try:
        _wait_until_up(f"http://127.0.0.1:{PORT}")

        def post_alert(i):
            t0 = time.perf_counter()
            r = requests.post(
                f"{base}/alerts/",
                json={
                    "station_id": SEED_STATION_ID,
                    "alert_type": "delay",
                    "message": f"verify-phase8 {i}",
                    "notify_email": True,
                    "notify_sms": True,
                },
                headers={"Authorization": f"Bearer {SEED_ADMIN_EMAIL}"},
                timeout=REQUEST_TIMEOUT,
            )
            return ("alert", r.status_code, time.perf_counter() - t0)

        def get_alerts(i):
            t0 = time.perf_counter()
            r = requests.get(
                f"{base}/alerts/",
                headers={"Authorization": f"Bearer {SEED_READER_EMAIL}"},
                timeout=REQUEST_TIMEOUT,
            )
            return ("read", r.status_code, time.perf_counter() - t0)

        baseline = [get_alerts(f"warm{i}")[2] for i in range(5)]
        print(f"warm baseline GET latencies: {[round(x, 3) for x in baseline]}")

        results = []
        with ThreadPoolExecutor(max_workers=N_ALERTS + N_READS) as pool:
            futures = [pool.submit(post_alert, i) for i in range(N_ALERTS)]
            time.sleep(0.05)
            futures += [pool.submit(get_alerts, i) for i in range(N_READS)]
            for fut in as_completed(futures):
                results.append(fut.result())

        reads = [r for r in results if r[0] == "read"]
        read_times = [r[2] for r in reads]
        read_codes = sorted(set(r[1] for r in reads))

        baseline_mean = statistics.mean(baseline)
        under_load_mean = statistics.mean(read_times)

        print(f"\n{N_READS} concurrent GET /alerts during a {N_ALERTS}-alert dispatch burst:")
        print(f"  status codes: {read_codes}")
        print(f"  mean={under_load_mean:.3f}s (baseline quiet mean={baseline_mean:.3f}s, "
              f"{under_load_mean / baseline_mean:.1f}x)")

        ok = read_codes == [200] and under_load_mean < 5.0
        print("\nPASS" if ok else "\nFAIL", "- reads stayed healthy and fast under the dispatch burst"
              if ok else "- reads were delayed or errored under the dispatch burst")
        return 0 if ok else 1
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
