"""Stress-test app/core/cache.py's shared global _client under real
concurrent multi-thread access WHILE Redis flaps up/down repeatedly -
exactly the "Redis under concurrent load" scenario. Looks for races in
the unlocked read of `_client` in _get_client() and the fully unlocked
_mark_down(), which can otherwise let one thread hand out a client
object that a concurrently-running _mark_down() on another thread is
simultaneously invalidating/closing.
"""
import os, subprocess, sys, threading, time

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "dummy")
os.environ.setdefault("CACHE_ENABLED", "True")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core import cache

stop = threading.Event()
errors = []
errors_lock = threading.Lock()
ops = {"n": 0}
ops_lock = threading.Lock()

def hammer(tid):
    i = 0
    while not stop.is_set():
        try:
            cache.set_json(f"k{tid}-{i}", {"v": i})
            cache.get_json(f"k{tid}-{i}")
        except Exception as exc:
            with errors_lock:
                errors.append(f"thread{tid}: {type(exc).__name__}: {exc}")
        i += 1
        with ops_lock:
            ops["n"] += 1

def flapper():
    while not stop.is_set():
        time.sleep(0.15)
        subprocess.run(["redis-cli", "shutdown", "nosave"], capture_output=True)
        time.sleep(0.1)
        subprocess.Popen(["redis-server", "--daemonize", "yes", "--port", "6379"])

threads = [threading.Thread(target=hammer, args=(t,)) for t in range(16)]
flap = threading.Thread(target=flapper, daemon=True)

for th in threads: th.start()
flap.start()
time.sleep(6.0)
stop.set()
for th in threads: th.join(timeout=5)

# make sure redis is actually back up for anything after this
subprocess.Popen(["redis-server", "--daemonize", "yes", "--port", "6379"])
time.sleep(1.0)

print(f"total ops attempted: {ops['n']}")
print(f"unhandled exceptions leaking out of cache.get_json/set_json: {len(errors)}")
for e in errors[:15]:
    print("  [FAIL]", e)
print("\nOVERALL:", "FAIL" if errors else "PASS")
sys.exit(1 if errors else 0)
