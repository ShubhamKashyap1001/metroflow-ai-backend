"""Operational metrics, exposed in Prometheus text format at GET
/metrics (wired up in app/main.py).

Scope is deliberately narrow - exactly two things:

1. API latency/error metrics - `http_requests_total` (a Counter,
   labeled by method/route/status) and `http_request_duration_seconds`
   (a Histogram, labeled by method/route), both recorded by the
   `metrics_middleware` added in app/main.py for every HTTP request
   handled by the app.

2. PostgreSQL/Redis failure metrics - `db_failures_total` and
   `redis_failures_total`, both Counters labeled only by a short,
   fixed `reason` (an exception class name, e.g. "OperationalError",
   or a small fixed string like "pool_exhausted") - see
   record_db_failure()/record_redis_failure() below for where each is
   incremented.

No secrets or sensitive data, by construction:
  - Route labels use the FastAPI/Starlette *route template*
    (e.g. "/api/v1/stations/{station_id}"), never the resolved
    request path - so a path segment that happens to be a user id,
    email, or token never becomes a metric label. Unmatched paths
    (404s, scanner/bot noise) are folded into a single "unmatched"
    label instead of being recorded verbatim, for the same reason and
    to keep cardinality bounded.
  - Failure labels are exception *class names* or short fixed reason
    strings only - never str(exc)/repr(exc)/exc.args, which for a DB
    or Redis connectivity error can contain the connection string
    (host, port, database name, and sometimes the username). The full
    exception still goes to the server log (via logger.error(...,
    exc_info=exc) at each call site below and in app/main.py's/
    app/api/v1/health.py's existing exception handling) - just never
    into a metric label, since metric labels/values are meant to be
    scraped and displayed on a dashboard, a much wider audience than
    the server log.
"""
import logging

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

logger = logging.getLogger(__name__)

# --- 1. API latency/error metrics ------------------------------------

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled, by method/route/status.",
    ["method", "route", "status"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds, by method/route.",
    ["method", "route"],
    # A wider low end than the default buckets: most of this API's
    # reads are sub-100ms, but a few (crowd dashboard aggregation,
    # AI prediction endpoints) legitimately take longer - these
    # buckets resolve both without needing a second histogram.
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

def route_label(request) -> str:
    """The route TEMPLATE for `request` (e.g.
    "/api/v1/stations/{station_id}"), not the resolved path - see this
    module's docstring for why. Falls back to "unmatched" for anything
    that never matched a route (404s, scanner traffic hitting random
    paths) so those can't blow up label cardinality or end up recorded
    verbatim.

    Built from `request.url.path` (the actual, fully-qualified path
    the client requested, prefix included) with each matched path
    parameter's concrete VALUE substituted back to "{param_name}" via
    `request.path_params` - rather than trusting a route object's own
    `.path` attribute to already include every prefix an app's
    `include_router(prefix=...)` calls added, which nested routers
    don't always reflect consistently. Substituting by value is
    simpler and version-proof: whatever the resolved path looked
    like, every concrete id/slug the router actually captured as a
    path parameter gets scrubbed back out of it before it's ever used
    as a label.
    """
    if request.scope.get("route") is None:
        return "unmatched"
    path = request.url.path
    for name, value in request.path_params.items():
        value_str = str(value)
        if value_str:
            path = path.replace(value_str, "{" + name + "}")
    return path

# --- 2. PostgreSQL/Redis failure metrics -----------------------------

DB_FAILURES_TOTAL = Counter(
    "db_failures_total",
    "PostgreSQL failures, by reason (exception class name, or a short "
    "fixed reason like 'pool_exhausted' for a connection-pool timeout "
    "that never reached the database at all).",
    ["reason"],
)

REDIS_FAILURES_TOTAL = Counter(
    "redis_failures_total",
    "Redis cache failures, by reason (exception class name).",
    ["reason"],
)

def record_db_failure(reason: str) -> None:
    """Increment db_failures_total. `reason` must be a short, fixed
    label value (an exception class name or a constant like
    "pool_exhausted") - never str(exc)/repr(exc), which can contain
    the connection string. See app/database/database.py's `handle_error`
    engine event (actual DBAPI/query failures) and
    app/main.py's db_pool_exhausted_handler (pool checkout timeout,
    which is raised by SQLAlchemy's own pool before any DBAPI call is
    attempted, so it never reaches the `handle_error` event) for the
    two call sites.
    """
    DB_FAILURES_TOTAL.labels(reason=reason).inc()

def record_redis_failure(reason: str) -> None:
    """Increment redis_failures_total. `reason` must be a short, fixed
    label value (an exception class name) - never str(exc)/repr(exc).
    See app/core/cache.py's `_mark_down()`, the single place every
    Redis operation in this module already funnels a failure through.
    """
    REDIS_FAILURES_TOTAL.labels(reason=reason).inc()

# --- 3. WebSocket connection/reconnect/event-failure metrics ---------
# --- 4. Simulator leader/heartbeat monitoring -------------------------
#
# Both are exposed via one custom Collector (see prometheus_client's
# Collector protocol) that PULLS its numbers from
# app/websocket/manager.py's ConnectionManager and
# app/simulator/leader_election.py's LeaderElection instances at
# scrape time, rather than having those modules import this one to
# `.inc()` a stateful Counter directly.
#
# Why pull instead of push: both of those modules are required to
# stay importable with nothing beyond fastapi/redis/app.core.cache/
# app.simulator.local_lock in their own offline verification
# harnesses (scripts/verify_ws_*.py, scripts/verify_leader_election*.py
# - see those scripts' module docstrings), which inject fake stand-ins
# for exactly those dependencies and nothing else. If either module
# imported this one to push updates, importing them would also
# require prometheus_client to be installed, breaking every one of
# those harnesses for no functional benefit. Pulling instead means
# this is the only module that ever needs prometheus_client for this
# feature; manager.py/leader_election.py just keep plain, lock-guarded
# ints/dicts (see their own get_metrics_snapshot() methods) that this
# collector reads once per scrape and republishes as real Prometheus
# series. The import of those two modules below is deliberately done
# INSIDE collect(), not at the top of this file, so this module never
# has to be imported before they are (collect() only ever runs after
# the app has fully started and every route/service has already been
# imported).
#
# Same no-secrets guarantee as the rest of this file: every label used
# below is a short, fixed-vocabulary string - a WebSocket event name
# from app/websocket/events.py, a disconnect reason from a small fixed
# set ("client_close"/"error"/"stale_reaped"/"send_failed"/"unknown"),
# or a background loop's own fixed name - never anything derived from
# request/connection data.

_LEADER_STATE_VALUES = {"not_started": 0, "standby": 1, "leader": 2, "crashed": 3}

class _RealtimeCollector:
    """Registered once, below, with the default REGISTRY.

    Implements `describe()` (returning no metrics) so that
    `REGISTRY.register()` never calls `collect()` at registration time.
    The default global REGISTRY is created with `auto_describe=True`,
    so without a `describe()` override, `register()` itself calls
    `collect()` once (via `_get_names()`, to check for duplicate metric
    names) - immediately, synchronously, as part of importing this
    module. That defeats the entire point of the lazy imports inside
    `collect()` documented above ("this module never has to be
    imported before they are"): it turns them back into eager,
    import-time imports of app.simulator.scheduler and
    app.websocket.manager, which - if anything else is mid-import of
    *this* module's own import chain at that moment (e.g. code that
    imports app.websocket.manager, which imports app.core.cache,
    which imports app.core.metrics) - crashes with a circular-import
    ImportError, since app.websocket.manager would still be
    partially initialized (its own `manager = ConnectionManager()`
    line hasn't run yet). Returning an empty list here is the
    standard prometheus_client idiom for "this collector's real
    collect() is expensive/has side effects; don't call it just to
    learn metric names" - duplicate-name detection still works fine
    at actual scrape time via the real collect() below.
    """

    def describe(self):
        return []

    def collect(self):
        from app.simulator import scheduler as simulator_scheduler
        from app.websocket.manager import manager as ws_manager

        ws_snapshot = ws_manager.get_metrics_snapshot()

        connects = CounterMetricFamily(
            "ws_connections_total",
            "Total WebSocket connections accepted at /ws/monitor. A "
            "reconnect looks identical to a first-time connect from "
            "the server's side (a dropped socket carries no identity "
            "a new handshake could resume), so reconnect churn is "
            "read off this alongside ws_disconnects_total rather than "
            "as a separate counter.",
        )
        connects.add_metric([], ws_snapshot["connects_total"])
        yield connects

        disconnects = CounterMetricFamily(
            "ws_disconnects_total",
            "Total WebSocket disconnects, by reason: client_close "
            "(clean client-initiated close), error (the receive loop "
            "raised something other than a clean disconnect), "
            "stale_reaped (dropped by the liveness reaper for going "
            "quiet), send_failed (dropped after a broadcast send to "
            "it failed or timed out).",
            labels=["reason"],
        )
        for reason, count in ws_snapshot["disconnects_total"].items():
            disconnects.add_metric([reason], count)
        yield disconnects

        active = GaugeMetricFamily(
            "ws_active_connections",
            "WebSocket connections currently open on this process.",
        )
        active.add_metric([], ws_snapshot["active_connections"])
        yield active

        send_failures = CounterMetricFamily(
            "ws_event_send_failures_total",
            "Total failed/timed-out sends of a broadcast event to an "
            "individual WebSocket connection, by event name (see "
            "app/websocket/events.py). The connection is dropped "
            "immediately after (see ws_disconnects_total{reason="
            '"send_failed"}).',
            labels=["event"],
        )
        for event, count in ws_snapshot["event_send_failures_total"].items():
            send_failures.add_metric([event], count)
        yield send_failures

        heartbeats = CounterMetricFamily(
            "simulator_leader_heartbeats_total",
            "Total successful leadership-lease heartbeats (a Redis "
            "lease acquire-or-renew, or the same-host local-lock "
            "fallback when Redis is unavailable - see "
            "app/simulator/leader_election.py) recorded on this "
            "process for a named background loop, one per election "
            "tick while leading it.",
            labels=["loop"],
        )
        acquired = CounterMetricFamily(
            "simulator_leadership_acquired_total",
            "Total times this process became the leader for a named "
            "background loop.",
            labels=["loop"],
        )
        lost = CounterMetricFamily(
            "simulator_leadership_lost_total",
            "Total times this process stepped down (or lost the "
            "lease/lock) for a named background loop.",
            labels=["loop"],
        )
        crashes = CounterMetricFamily(
            "simulator_worker_crashes_total",
            "Total times a named background loop's worker task exited "
            "with an unhandled exception while this process was "
            "leading it.",
            labels=["loop"],
        )
        last_heartbeat = GaugeMetricFamily(
            "simulator_leader_last_heartbeat_timestamp_seconds",
            "Unix timestamp of the most recent successful leadership "
            "heartbeat for a named loop on this process (0 if this "
            "process has never held leadership for it). Compare "
            "against time() to alert on a leader that has gone silent "
            "without stepping down.",
            labels=["loop"],
        )
        state = GaugeMetricFamily(
            "simulator_leader_state",
            "Current leader-election state for a named background "
            "loop on this process: 0=not_started, 1=standby, "
            "2=leader, 3=crashed. Standby is healthy and expected in "
            "a multi-worker deployment - it means another process "
            "currently holds leadership for that loop.",
            labels=["loop"],
        )
        for snapshot in simulator_scheduler.scheduler_metrics_snapshot():
            loop = [snapshot["name"]]
            heartbeats.add_metric(loop, snapshot["heartbeat_ticks_total"])
            acquired.add_metric(loop, snapshot["leadership_acquired_total"])
            lost.add_metric(loop, snapshot["leadership_lost_total"])
            crashes.add_metric(loop, snapshot["worker_crashes_total"])
            last_heartbeat.add_metric(loop, snapshot["last_heartbeat_ts"] or 0)
            state.add_metric(loop, _LEADER_STATE_VALUES.get(snapshot["state"], 0))
        yield heartbeats
        yield acquired
        yield lost
        yield crashes
        yield last_heartbeat
        yield state

REGISTRY.register(_RealtimeCollector())

def render_latest() -> tuple[bytes, str]:
    """(body, content_type) for the /metrics endpoint - see
    app/main.py. A thin wrapper so main.py doesn't need to import
    prometheus_client directly."""
    return generate_latest(), CONTENT_TYPE_LATEST
