# Rate limiting

Covers request rate limiting: enforcement, tiering, and making it work
correctly across multiple API workers via shared storage. WebSocket
traffic is explicitly out of scope (see below for why it structurally
can't be covered by the same mechanism).

## What was broken

1. **Global limits were dead code.** `main.py` constructed a
   `Limiter(..., default_limits=["100/minute"])` and set it as
   `app.state.limiter`, but never registered `SlowAPIMiddleware`.
   Without that middleware, slowapi never checks `default_limits` (or
   any `@limiter.limit(...)` decorator) on a request — so in practice
   almost every write endpoint (check-in/out, alerts, enquiries,
   stations, trains, schedules, news, user profile updates,
   notifications), the admin simulator/train-tracker toggles, and the
   auth endpoint had **zero** rate limiting.
2. **Not shared across workers.** `main.py`, `app/api/v1/prediction.py`,
   and `app/api/v1/analytics.py` each instantiated their own
   `Limiter()`. Every `Limiter()` defaults to in-process
   `MemoryStorage`, so under multiple worker processes each one
   counted independently — a client could get up to N× quota just by
   landing on different workers, and every counter reset on process
   restart.

## What was fixed

**`app/core/rate_limit.py`** — one shared `Limiter`, backed by Redis
via `storage_uri` (`settings.REDIS_URL`, already a project dependency).
If Redis is unreachable at import time, it falls back to in-memory
storage with a logged warning instead of crashing the app on startup —
the same fail-open pattern `app.core.cache` already uses for the same
dependency. Every router imports `limiter` from here; nothing
constructs its own `Limiter()` anymore.

**`main.py`** imports the shared `limiter`, sets it as
`app.state.limiter`, and registers `SlowAPIMiddleware` so
`default_limits` and every `@limiter.limit(...)` decorator are
actually enforced.

**Tiered limits**, applied per category, keyed by source IP
(`slowapi.util.get_ipaddr`):

| Tier | Limit | Applies to |
|---|---|---|
| `AUTH_LIMIT` | 30/min | `/auth/me` (token verification) |
| `WRITE_LIMIT` | 30/min | check-in/out, crowd ingest, stations, trains, schedules, news, enquiries, alerts, user updates, notifications |
| `ADMIN_LIMIT` | 10/min | simulator / train-tracker on-off toggles |
| `AI_LIMIT` | 20/min | `prediction.py` + `analytics.py` (model-backed endpoints) — value unchanged from before this work, now on shared storage |
| `DEFAULT_LIMIT` | 100/min | global floor for every other route — value unchanged, now actually enforced |

The AI tier's 20/min limit is the same one
[ai-recommendations.md](./ai-recommendations.md) fixed the frontend's
request pattern against — the frontend now calls the bulk
recommendations endpoint once instead of one call per station, so it
doesn't collide with this tier even at the same limit.

## WebSocket traffic is deliberately left alone

`SlowAPIMiddleware` subclasses Starlette's `BaseHTTPMiddleware`, which
only wraps `"http"`-scope ASGI calls. A `"websocket"`-scope connection
(`/ws/monitor`) bypasses `BaseHTTPMiddleware` entirely at the ASGI
level and never reaches `dispatch()`. Registering the middleware does
not, and structurally cannot, rate-limit the WebSocket handshake or
the messages sent over it — confirmed by test, not just by reading the
ASGI spec (`test_websocket_not_rate_limited_by_default_limits`,
`test_websocket_handshake_unaffected_by_http_rate_limit_state`). No
websocket-specific exemption code was needed, or possible to get
wrong — there's simply nothing to exempt.

## Tests

`tests/test_rate_limiting.py`:

- `test_auth_me_returns_429_past_its_limit` — auth tier trips at 30/min
- `test_write_endpoint_returns_429_past_its_limit` — write tier trips at 30/min
- `test_ai_prediction_endpoint_returns_429_past_its_limit` — AI tier trips at 20/min
- `test_admin_endpoint_uses_stricter_shared_limit` — admin tier trips at 10/min
- `test_429_response_has_useful_shape` — 429 body/headers are usable by a client
- `test_limits_are_shared_across_separate_limiter_instances` — the
  actual multi-worker regression test: two independent `Limiter`
  instances pointed at the same Redis prove counters are shared, not
  per-process
- `test_websocket_not_rate_limited_by_default_limits` — high WS
  message volume is not throttled even after the HTTP default limit is
  exhausted on the same client
- `test_websocket_handshake_unaffected_by_http_rate_limit_state` — WS
  still connects after HTTP limits are exhausted

## Verification

- AST-checked every `@limiter.limit(...)`-decorated route across
  `app/api/v1/*.py` for the `request: Request` parameter slowapi needs
  to read the client key — all present.
- Confirmed no orphaned `Limiter()` instantiations remain anywhere in
  `app/`, and `SlowAPIMiddleware` is registered exactly once in
  `main.py`.
