import logging
import time

import redis
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_ipaddr

from app.core.config import settings

logger = logging.getLogger(__name__)

AUTH_LIMIT = "30/minute"     
WRITE_LIMIT = "30/minute"    
ADMIN_LIMIT = "10/minute"    
AI_LIMIT = "20/minute"       
DEFAULT_LIMIT = "100/minute"  

_REDIS_SOCKET_TIMEOUT_SECONDS = 1

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
        storage_uri=storage_uri,  

        storage_options=_STORAGE_OPTIONS,

    )

def _rate_limit_exceeded_handler_with_retry_after(request, exc):
    from starlette.responses import JSONResponse

    response = JSONResponse(
        {"error": f"Rate limit exceeded: {exc.detail}"}, status_code=429
    )
    current_limit = getattr(request.state, "view_rate_limit", None)
    if current_limit is not None:
        try:
            window_stats = limiter.limiter.get_window_stats(
                current_limit[0], *current_limit[1]
            )
            reset_in = 1 + window_stats[0]
            response.headers["Retry-After"] = str(max(int(reset_in - time.time()), 0))
        except Exception:

            logger.warning(
                "[rate_limit] failed to compute Retry-After for a 429 "
                "response - returning the 429 without it.",
                exc_info=True,
            )
    return response

limiter = _build_limiter()

_last_backend_error_log_ts = 0.0

def _patched_check_request_limit(*args, **kwargs):
    global _last_backend_error_log_ts
    try:
        return _original_check_request_limit(*args, **kwargs)
    except RateLimitExceeded:

        raise
    except Exception:
        now = time.time()
        if now - _last_backend_error_log_ts > 30:
            _last_backend_error_log_ts = now
            logger.warning(
                "[rate_limit] backend error during rate-limit check "
                "(Redis unavailable or over quota/memory) - failing "
                "OPEN for this request instead of blocking it.",
                exc_info=True,
            )
        return None

_original_check_request_limit = limiter._check_request_limit
limiter._check_request_limit = _patched_check_request_limit
