import logging

from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

logger = logging.getLogger(__name__)

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled, by method/route/status.",
    ["method", "route", "status"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds, by method/route.",
    ["method", "route"],

    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)

def route_label(request) -> str:
    if request.scope.get("route") is None:
        return "unmatched"
    path = request.url.path
    for name, value in request.path_params.items():
        value_str = str(value)
        if value_str:
            path = path.replace(value_str, "{" + name + "}")
    return path

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
    DB_FAILURES_TOTAL.labels(reason=reason).inc()

def record_redis_failure(reason: str) -> None:
    REDIS_FAILURES_TOTAL.labels(reason=reason).inc()

_LEADER_STATE_VALUES = {"not_started": 0, "standby": 1, "leader": 2, "crashed": 3}

class _RealtimeCollector:

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
    return generate_latest(), CONTENT_TYPE_LATEST
