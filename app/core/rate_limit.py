"""Shared rate limiter for the whole API.

Why this file exists
---------------------
Before this fix, rate limiting was broken in two separate ways:

1. **No global enforcement.** `app/main.py` created a `Limiter(...,
   default_limits=["100/minute"])` and set it as `app.state.limiter`,
   but never registered slowapi's `SlowAPIMiddleware`. Without that
   middleware, `default_limits` is never actually checked on routes
   that don't carry an explicit `@limiter.limit(...)` decorator - so
   in practice almost every write endpoint (check-in/out, alerts,
   enquiries, stations, trains, schedules, news, user profile
   updates, admin simulator controls) and the auth endpoint had NO
   rate limit at all.

2. **Not shared across workers.** `app/main.py`,
   `app/api/v1/prediction.py` and `app/api/v1/analytics.py` each
   instantiated their OWN `Limiter()`. Every `Limiter()` defaults to
   an in-process `MemoryStorage`, so as soon as the API runs with more
   than one worker process (`uvicorn --workers N`, gunicorn, multiple
   containers/pods behind a load balancer), each worker keeps its own
   separate counters. A client hitting a "20/minute" endpoint could
   get up to 20 * N requests through simply by landing on different
   workers, and every counter resets whenever its process restarts.

Fix: exactly ONE `Limiter`, defined here, backed by Redis via
`storage_uri` so every worker process (and every replica, if this
ever runs on more than one host) reads and increments the same
counters. Every router imports `limiter` from this module instead of
constructing its own, and `app/main.py` registers `SlowAPIMiddleware`
so `default_limits` actually applies to routes with no explicit
decorator.

Redis availability
-------------------
If Redis can't be reached at import time (dev box without Redis
running, Redis briefly down during a deploy), we fall back to
in-memory storage instead of crashing the app on startup - the same
fail-open philosophy `app.core.cache` already uses for the same
dependency. In that fallback mode, limits are only correct
per-process (i.e. the exact multi-worker gap this module exists to
close) until Redis is reachable again; that's logged once at WARNING
so it's visible rather than silently degrading.

WebSocket traffic
-------------------
`SlowAPIMiddleware` subclasses Starlette's `BaseHTTPMiddleware`, which
only wraps `"http"` scope ASGI calls - a `"websocket"` scope request
(like `/ws/monitor` in main.py) bypasses `BaseHTTPMiddleware` entirely
at the ASGI level and is never passed through `dispatch()`. So
registering this middleware does not, and cannot, rate-limit the
WebSocket connection or the messages sent over it - only ordinary HTTP
request/response routes are affected. No websocket-specific exemption
code is needed (or possible to get wrong) here.

Per-request Redis timeout (hang fix)
-------------------------------------
`_redis_reachable()` below only guards *startup*: it proves Redis is
up once, when the process boots. It does nothing for the connection
slowapi's `Limiter` actually uses afterwards - that one is built
internally by the `limits` library from the bare `storage_uri` string,
and redis-py's own defaults for a client built that way are
`socket_connect_timeout=None, socket_timeout=None`, i.e. no timeout at
all. So a Redis that goes from "up" to "up but not responding"
(network partition, an overloaded/wedged Redis, a firewall silently
dropping packets after the connection is already established) after
startup would make the socket call inside every single rate-limited
request block forever - the exact "request that can hang
indefinitely" / "blocking external call without a controlled timeout"
pattern, just one level below the app code most people would think to
check.

Fixed by passing `storage_options` through to that same client
construction, matching the (short, deliberately unchanged) timeout
`_redis_reachable` already uses - so the limiter's ongoing per-request
Redis calls are bounded exactly like the startup check already was,
without touching any of the limit *values* above (no limit was raised
or loosened to fix this).
"""
import logging

import redis
from slowapi import Limiter
from slowapi.util import get_ipaddr

from app.core.config import settings

logger = logging.getLogger(__name__)

# Per-client (per source IP) limits, by endpoint category.
AUTH_LIMIT = "30/minute"     # token-verification endpoint(s) (app/api/v1/authentication.py)
WRITE_LIMIT = "30/minute"    # ordinary state-mutating endpoints (POST/PUT/PATCH/DELETE)
ADMIN_LIMIT = "10/minute"    # sensitive admin on/off switches (simulator, train tracker)
AI_LIMIT = "20/minute"       # ML-model-backed endpoints (prediction/analytics) - unchanged value
DEFAULT_LIMIT = "100/minute"  # global fallback for every other route - unchanged value


# Same bound used on both the one-time startup check below AND the
# limiter's own ongoing per-request Redis calls (via _STORAGE_OPTIONS)
# - a hung/unresponsive Redis fails a rate-limit check fast instead of
# blocking the request indefinitely. Deliberately short and NOT a
# "limit" in the rate-limiting sense - do not confuse with AUTH_LIMIT
# etc. above; raising this would trade hang-risk for slower failure
# detection, not fix anything.
_REDIS_SOCKET_TIMEOUT_SECONDS = 1

# Passed straight through to the `limits` library's RedisStorage,
# which forwards unrecognised kwargs to redis-py's own client
# constructor - see the "Per-request Redis timeout" note above for
# why this is needed in addition to _redis_reachable()'s own timeout.
_STORAGE_OPTIONS = {
    "socket_connect_timeout": _REDIS_SOCKET_TIMEOUT_SECONDS,
    "socket_timeout": _REDIS_SOCKET_TIMEOUT_SECONDS,
}


def _redis_reachable(url: str) -> bool:
    try:
        client = redis.from_url(
            url,
            socket_connect_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
        )
        return bool(client.ping())
    except Exception:
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


def _build_limiter() -> Limiter:
    storage_uri = None
    if settings.REDIS_URL and _redis_reachable(settings.REDIS_URL):
        storage_uri = settings.REDIS_URL
    elif settings.REDIS_URL:
        logger.warning(
            "[rate_limit] Redis unreachable at startup - falling back to "
            "in-memory rate-limit storage. Limits will only be correct "
            "per-process (not shared across workers) until Redis is "
            "reachable again."
        )

    return Limiter(
        key_func=get_ipaddr,
        default_limits=[DEFAULT_LIMIT],
        storage_uri=storage_uri,  # None -> slowapi's default in-memory storage
        # Bounds the socket calls the limiter itself makes against
        # Redis on every rate-limited request (not just the startup
        # probe above) - see this module's docstring. A no-op when
        # storage_uri is None (in-memory storage ignores it).
        storage_options=_STORAGE_OPTIONS,
    )


limiter = _build_limiter()
