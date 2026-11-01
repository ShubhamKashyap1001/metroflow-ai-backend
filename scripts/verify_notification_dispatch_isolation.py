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
