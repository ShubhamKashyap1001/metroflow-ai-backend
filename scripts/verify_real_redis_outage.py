"""Real Redis process kill/restart mid-load, not a simulated bus.
Two ConnectionManagers (two simulated workers) against a REAL local
redis-server that we actually kill (SIGKILL via `redis-cli shutdown
nosave`) and restart while publishing continuously. Checks: does the
relay recover on its own, and are messages sent *after* recovery
delivered without duplication?
"""
import asyncio, json, os, subprocess, sys, time, threading

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")
os.environ.setdefault("CACHE_ENABLED", "True")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.websocket.manager import ConnectionManager
from app.websocket import events
from app.core import cache

class FakeWS:
    def __init__(self, name):
        self.name = name; self.received = []
    async def accept(self): pass
    async def send_text(self, payload):
        self.received.append(json.loads(payload))

async def run():
    loop = asyncio.get_event_loop()
    w1 = ConnectionManager(); w1.bind_loop(loop); w1.start_relay()
    w2 = ConnectionManager(); w2.bind_loop(loop); w2.start_relay()
    await asyncio.sleep(1.0)

    tabA = FakeWS("A-on-w1"); await w1.connect(tabA)
    tabB = FakeWS("B-on-w2"); await w2.connect(tabB)

    stop_flag = threading.Event()
    counter = {"i": 0}
    def publisher():
        while not stop_flag.is_set():
            i = counter["i"]; counter["i"] += 1
            w1.notify(events.STATION_ALERT, {"event_marker": f"e{i}"})
            time.sleep(0.02)
    pub_thread = threading.Thread(target=publisher, daemon=True)
    pub_thread.start()

    await asyncio.sleep(1.0)
    print("--- killing redis-server mid-load ---")
    subprocess.run(["redis-cli", "shutdown", "nosave"], capture_output=True)
    await asyncio.sleep(3.0)
    print("--- restarting redis-server ---")
    subprocess.Popen(["redis-server", "--daemonize", "yes", "--port", "6379"])
    # wait for it to actually come up
    for _ in range(50):
        try:
            c = cache._build_client(); c.ping(); break
        except Exception:
            await asyncio.sleep(0.2)
    print("--- redis back up, continuing load for 5s ---")
    await asyncio.sleep(5.0)
    stop_flag.set()
    pub_thread.join()
    await asyncio.sleep(2.0)  # drain

    markers_a = [m["data"]["event_marker"] for m in tabA.received if "event_marker" in m.get("data", {})]
    markers_b = [m["data"]["event_marker"] for m in tabB.received if "event_marker" in m.get("data", {})]

    dup_a = len(markers_a) - len(set(markers_a))
    dup_b = len(markers_b) - len(set(markers_b))
    print(f"tabA (same process/worker as publisher): {len(markers_a)} received, {dup_a} duplicates")
    print(f"tabB (cross-process via relay): {len(markers_b)} received, {dup_b} duplicates")
    print(f"total published: {counter['i']}")

    # tabA is LOCAL to the publishing worker - it must receive
    # every single one, outage or not (local delivery never depends
    # on Redis).
    missing_a = counter["i"] - len(set(markers_a))
    ok = True
    if missing_a != 0:
        print(f"[FAIL] tabA (local delivery) missing {missing_a} events - local delivery must never depend on Redis")
        ok = False
    if dup_a or dup_b:
        print("[FAIL] duplicate deliveries found")
        ok = False
    # tabB necessarily misses whatever was published during the outage
    # window (pub/sub can't replay) - that's expected, not a bug -
    # but it MUST resume receiving live events after recovery with no
    # duplicates, which the checks above already establish.
    late_b = [m for m in markers_b if int(m[1:]) > counter["i"] - 30]
    if not late_b:
        print("[FAIL] tabB received nothing near the end - relay did not recover")
        ok = False
    else:
        print(f"[OK] tabB resumed receiving events after recovery (e.g. {late_b[:3]})")

    print("\nOVERALL:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    asyncio.run(run())
