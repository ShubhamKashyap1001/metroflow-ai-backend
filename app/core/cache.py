import json
import logging
import threading
import time
from collections import OrderedDict
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
_local_cache_lock = threading.Lock()
_local_cache: OrderedDict[str, tuple[str, float | None]] = OrderedDict()
_LOCAL_CACHE_MAX_ENTRIES = 5000

def _local_get_raw(key: str) -> str | None:
    now = time.monotonic()
    with _local_cache_lock:
        entry = _local_cache.get(key)
        if entry is None:
            return None
        raw, expires_at = entry
        if expires_at is not None and expires_at <= now:
            _local_cache.pop(key, None)
            return None
        _local_cache.move_to_end(key)
        return raw

def _local_set_raw(key: str, raw: str, ttl_seconds: int | None) -> None:
    expires_at = time.monotonic() + ttl_seconds if ttl_seconds is not None else None
    with _local_cache_lock:
        _local_cache[key] = (raw, expires_at)
        _local_cache.move_to_end(key)
        while len(_local_cache) > _LOCAL_CACHE_MAX_ENTRIES:
            _local_cache.popitem(last=False)

def _local_set_nx(key: str, raw: str, ttl_seconds: int) -> bool:
    now = time.monotonic()
    with _local_cache_lock:
        entry = _local_cache.get(key)
        if entry is not None:
            _, expires_at = entry
            if expires_at is None or expires_at > now:
                return False
        _local_cache[key] = (raw, now + ttl_seconds)
        _local_cache.move_to_end(key)
        while len(_local_cache) > _LOCAL_CACHE_MAX_ENTRIES:
            _local_cache.popitem(last=False)
        return True

def _local_delete(key: str) -> None:
    with _local_cache_lock:
        _local_cache.pop(key, None)

def _log_state_change(connected: bool, error: str | None = None) -> None:
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

    try:
        metrics.record_redis_failure(type(exc).__name__)
    except Exception:
        pass

def _mark_up() -> None:
    global _current_backoff
    _current_backoff = _BACKOFF_FLOOR_SECONDS
    _log_state_change(True)

def _get_client() -> "redis.Redis | None":
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
    client = _get_client()
    if client is None:
        raw = _local_get_raw(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            _local_delete(key)
            return None
    try:
        raw = client.get(key)
    except Exception as exc:                
        _mark_down(exc)
        raw = _local_get_raw(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None

def set_json(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    ttl = ttl_seconds if ttl_seconds is not None else settings.CACHE_TTL_SECONDS
    raw = json.dumps(value, default=str)
    _local_set_raw(key, raw, ttl)
    client = _get_client()
    if client is None:
        return
    try:
        client.set(key, raw, ex=ttl)
    except Exception as exc:                
        _mark_down(exc)

def set_json_persistent(key: str, value: Any) -> None:
    raw = json.dumps(value, default=str)
    _local_set_raw(key, raw, None)
    client = _get_client()
    if client is None:
        return
    try:
        client.set(key, raw)
    except Exception as exc:                
        _mark_down(exc)

def set_nx(key: str, ttl_seconds: int) -> bool:
    client = _get_client()
    if client is None:
        return _local_set_nx(key, "1", ttl_seconds)
    try:
        return bool(client.set(key, "1", nx=True, ex=ttl_seconds))
    except Exception as exc:                
        _mark_down(exc)
        return _local_set_nx(key, "1", ttl_seconds)

def delete(key: str) -> None:
    _local_delete(key)
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
