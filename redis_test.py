from dotenv import load_dotenv
import redis
import os

load_dotenv()

r = redis.from_url(
    os.getenv("REDIS_URL"),
    socket_connect_timeout=5,
    socket_timeout=5,
)

print("PING:", r.ping())
print("SET:", r.set("metroflow_test", "ok", ex=60))
print("GET:", r.get("metroflow_test"))

result = r.eval(
    "return redis.call('GET', KEYS[1])",
    1,
    "metroflow_test",
)

print("EVAL:", result)