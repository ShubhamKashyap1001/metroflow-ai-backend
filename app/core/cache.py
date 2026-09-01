"""Small Redis-backed cache helper.

Why this exists: `redis` has been sitting in requirements.txt but was
never actually wired into the app anywhere - no client, no
get/set calls. Meanwhile app/services/crowd_service.py's
get_station_wise_snapshot() (the dashboard/heatmap/congestion query)
runs a ROW_NUMBER() window query over the whole crowd_logs table on
every single request, and gets hit repeatedly per client (websocket
push + polling both trigger a refetch). This module gives that - and
any other hot read endpoint - a real cache in front of the DB.

Design choices:
  - get_json/set_json degrade to a no-op / cache-miss if Redis is
    unreachable (connection refused, DNS failure, timeout, etc.) so a
    missing/down Redis never takes the API down - it just falls back
    to hitting the DB every time, same as before this change.
  - A short, fixed TTL (settings.CACHE_TTL_SECONDS) is used instead of
    manual invalidation on write. The data this caches (live crowd
    counts) already changes every SIMULATOR_INTERVAL_SECONDS, so a TTL
    of the same size bounds staleness to what the UI already tolerates,
    without needing to track/invalidate a cache key per station per
    state filter on every write.

Milestone 16 - Redis Runtime Optimization
------------------------------------------
Two real production bugs existed here before this milestone, both
only visible under an actual Redis outage (not on a happy-path dev
box where Redis is always up):

1. `_client_init_attempted` was a one-shot flag. If Redis was down (or
   just not up yet - a container-startup race) the very first time
   any endpoint touched the cache, `_client` stayed `None` FOREVER -
   the app would never try Redis again for the rest of the process's
   life, even once Redis came back. There was no retry at all, let
   alone one with backoff.

2. `cache.get_json`/`set_json`/`delete` are called on nearly every
   request (auth's per-request profile lookup in core/security.py,
   the crowd dashboard, schedules, train positions, predictions all
   go through this module). Each one logged a `logger.warning(...)`
   on every single failure - so a down/timing-out Redis didn't just
   degrade quietly to the DB path as the module's own docstring
   claims, it also flooded the logs with one warning line per request
   per cache call, which is itself a production hazard (log volume,
   noise that buries real problems) and directly fails the
   milestone's "no Redis timeout logs" validation target.

Fix: a small reconnect state machine with exponential backoff.
  - While Redis is believed to be up, we use the client directly - no
    extra ping-per-call latency added.
  - The moment an operation fails, we mark Redis "down", close the
    dead client, log ONCE (state transition, at WARNING), and set a
    `next_retry_at` a short backoff away.
  - While down, every cache call is a monotonic-clock comparison and
    an immediate cache-miss/no-op - zero network I/O, zero log lines,
    so a sustained outage costs the request path nothing beyond what
    it already pays for the DB fallback (satisfies "never block
    requests").
  - Once `next_retry_at` has passed, the NEXT cache call gets to try
    reconnecting. Success resets the backoff to the floor and logs
    ONCE (state transition back to up, at INFO). A repeat failure
    silently doubles the backoff (capped) and does NOT log again -
    `_log_state_change` only fires on an actual up/down transition, so
    a sustained outage produces exactly two log lines for its entire
    duration (one when it starts, one when it ends) no matter how long
    it lasts or how many requests come in during it, instead of one
    warning per request.
  - Each individual redis-py call additionally uses redis-py's own
    built-in retry-with-exponential-backoff (`retry_on_timeout=True`,
    `retry_on_error=[...]`, `Retry(ExponentialBackoff(...), retries=1)`)
    so a single transient blip (one dropped packet, one slow GC pause
    on the Redis side) is retried once, sub-100ms, before we give up
    and fall through to the DB - without that one retry escalating
    into "mark the whole client down" for what was just a hiccup.
"""
import json
import logging
import threading
import time
from typing import Any

import redis
import redis.exceptions
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

from app.core import metrics
from app.core.config import settings

logger = logging.getLogger(__name__)

_PER_CALL_RETRIES = 1
_PER_CALL_BACKOFF = ExponentialBackoff(base=0.025, cap=0.1)

_BACKOFF_FLOOR_SECONDS = 1.0
_BACKOFF_CAP_SECONDS = 60.0

_client: "redis.Redis | None" = None
_lock = threading.Lock()
_next_retry_at = 0.0                                                    
_current_backoff = _BACKOFF_FLOOR_SECONDS
_last_state_connected: bool | None = None                                        
_last_error: str | None = None

def _log_state_change(connected: bool, error: str | None = None) -> None:
    """Log exactly once per up/down transition, never per-call."""
    global _last_state_connected, _last_error
    if _last_state_connected is connected:
        return
    _last_state_connected = connected
    _last_error = error
    if connected:
        logger.info("Redis connection restored.")
    else:
        logger.warning(
            "Redis unavailable (%s) - falling back to PostgreSQL-only "
            "reads until the next reconnect attempt in %.0fs. Set "
            "CACHE_ENABLED=False to silence this if Redis isn't "
            "installed in this environment.",
            error,
            _current_backoff,
        )

def _build_client() -> "redis.Redis":
    return redis.Redis.from_url(
        settings.REDIS_URL,
        socket_connect_timeout=1,
        socket_timeout=1,
        decode_responses=True,
        retry=Retry(_PER_CALL_BACKOFF, _PER_CALL_RETRIES),
        retry_on_timeout=True,
        retry_on_error=[redis.exceptions.ConnectionError, redis.exceptions.TimeoutError],
    )

def _mark_down(exc: Exception) -> None:
    """Record a failure, close the dead client, and schedule the next
    reconnect attempt with exponential backoff (capped)."""
    global _client, _next_retry_at, _current_backoff
    if _client is not None:
        try:
            _client.close()
        except Exception:                                           
            pass
    _client = None
    _next_retry_at = time.monotonic() + _current_backoff
    _log_state_change(False, repr(exc))
    _current_backoff = min(_current_backoff * 2, _BACKOFF_CAP_SECONDS)
    # Redis failure metric (see app/core/metrics.py) - only the
    # exception's class name is used as the label, never repr(exc)
    # (already logged above, server-side only) which can contain the
    # Redis connection string/credentials. Every get_json/set_json/
    # set_nx/delete failure funnels through this one function, so this
    # is the single place that needs to record it.
    try:
        metrics.record_redis_failure(type(exc).__name__)
    except Exception:
        pass

def _mark_up() -> None:
    global _current_backoff
    _current_backoff = _BACKOFF_FLOOR_SECONDS
    _log_state_change(True)

def _get_client() -> "redis.Redis | None":
    """Return a live Redis client, or None if caching is disabled or
    Redis is currently believed to be down (and it isn't time to
    retry yet). Never raises."""
    global _client

    if not settings.CACHE_ENABLED or not settings.REDIS_URL:
        return None

    if _client is not None:
        return _client

    now = time.monotonic()
    if now < _next_retry_at:
                                                                      
        return None

    with _lock:
                                                                   
        if _client is not None:
            return _client
        if time.monotonic() < _next_retry_at:
            return None

        try:
            candidate = _build_client()
            candidate.ping()
        except Exception as exc:                                             
            _mark_down(exc)
            return None

        _client = candidate
        _mark_up()
        return _client

def get_json(key: str) -> Any | None:
    """Return the cached value for `key`, or None on miss/any error."""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = client.get(key)
    except Exception as exc:                
        _mark_down(exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None

def set_json(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    """Cache `value` (JSON-serialisable) under `key`. Best-effort."""
    client = _get_client()
    if client is None:
        return
    try:
        client.set(
            key,
            json.dumps(value, default=str),
            ex=ttl_seconds if ttl_seconds is not None else settings.CACHE_TTL_SECONDS,
        )
    except Exception as exc:                
        _mark_down(exc)

def set_nx(key: str, ttl_seconds: int) -> bool:
    """Atomically set `key` to a sentinel value with an expiry, but only
    if it doesn't already exist (Redis `SET key val NX EX ttl`).

    Returns True if this call won the race (the key was absent and is
    now set - the caller should proceed with whatever it's guarding),
    or False if the key was already present (someone else already did
    that work recently - the caller should skip it).

    This is a *write* dedupe guard, distinct from get_json/set_json's
    *value* cache above - e.g. "don't insert another DB row for this
    station for the next N seconds" rather than "don't recompute this
    value for the next N seconds" (added for prediction_service's
    smart_recommendations() write-storm fix - Phase 1, P2-3).

    Fails OPEN (returns True) on a disabled/unreachable Redis, same
    fail-open philosophy as the rest of this module: a down cache never
    blocks the underlying write, it just loses the de-dup optimization
    for the duration of the outage - identical to write behaviour
    before this helper existed.
    """
    client = _get_client()
    if client is None:
        return True
    try:
        return bool(client.set(key, "1", nx=True, ex=ttl_seconds))
    except Exception as exc:                
        _mark_down(exc)
        return True

def delete(key: str) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.delete(key)
    except Exception as exc:                
        _mark_down(exc)

def delete_key(key: str) -> None:
    delete(key)

def get_client() -> "redis.Redis | None":
    """Expose the same managed, auto-reconnecting client used above, for
    callers that need a Redis primitive this module doesn't already wrap
    (Lua scripts, pub/sub). Same fail-open contract as get_json/set_json:
    returns None if caching is disabled or Redis is currently believed to
    be down - never raises. Added for Milestone/Phase 4 (leader election
    lock + the WebSocket cross-process relay), see leader_election.py and
    websocket/manager.py."""
    return _get_client()

                                                                     
_ACQUIRE_OR_RENEW_LOCK_SCRIPT = """
local current = redis.call('GET', KEYS[1])
if current == false or current == ARGV[1] then
    redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
    return 1
else
    return 0
end
"""

_RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('DEL', KEYS[1])
    return 1
else
    return 0
end
"""

def try_acquire_or_renew_lock(key: str, holder_id: str, ttl_seconds: int) -> bool:
    """Atomic compare-and-set lease acquire/renew (Redis `SET key val NX`
    generalised to also let the CURRENT holder renew its own lease without
    a gap). Returns True if `holder_id` now holds the lease (either it was
    free, or `holder_id` already held it and just extended it), False if
    someone else currently holds it.

    This is the primitive app/simulator/leader_election.py polls on to
    guarantee only one worker process runs a given background loop at a
    time: the lease has a TTL, so a crashed holder's lease expires on its
    own (automatic failover) instead of needing an explicit crash-detection
    mechanism.

    Uses a Lua script (EVAL) so the GET-then-SET is atomic - two processes
    racing to acquire the same free key can never both succeed."""
    client = _get_client()
    if client is None:
        return False
    try:
        result = client.eval(_ACQUIRE_OR_RENEW_LOCK_SCRIPT, 1, key, holder_id, ttl_seconds)
    except Exception as exc:                
        _mark_down(exc)
        return False
    return bool(result)

def release_lock(key: str, holder_id: str) -> bool:
    """Best-effort early release of a lease held by `holder_id` (e.g. on
    graceful shutdown, so the next election doesn't have to wait out the
    full TTL). Only deletes the key if `holder_id` is still the current
    holder - never releases a lease someone else already won."""
    client = _get_client()
    if client is None:
        return False
    try:
        result = client.eval(_RELEASE_LOCK_SCRIPT, 1, key, holder_id)
    except Exception as exc:                
        _mark_down(exc)
        return False
    return bool(result)

def redis_status() -> dict:
    """Structured Redis health snapshot for the /health endpoint (Feature
    8 audit item: "verify Redis fallback" needs to be observable, not
    just implied by get_json()/set_json() failing silently). Distinguishes
    "disabled by config" from "enabled but unreachable" from "connected",
    since only the middle one is actually an operational problem - the
    other two are both valid states where the app runs fine on the
    PostgreSQL-only fallback path.

    Milestone 16: also reports the current reconnect backoff so a down
    Redis is observable as "retrying every Ns", not just a flat
    "unreachable" with no sense of whether/when it'll self-heal.
    """
    if not settings.CACHE_ENABLED or not settings.REDIS_URL:
        return {"connected": False, "state": "disabled"}

    client = _get_client()
    if client is not None:
        return {"connected": True, "state": "connected"}

    now = time.monotonic()
    retry_in = max(0.0, round(_next_retry_at - now, 1))
    return {
        "connected": False,
        "state": "unreachable",
        "error": _last_error,
        "retry_in_seconds": retry_in,
        "backoff_seconds": round(_current_backoff, 1),
    }
