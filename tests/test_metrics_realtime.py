
from prometheus_client.parser import text_string_to_metric_families

from app.core import metrics
from app.simulator import scheduler
from app.websocket.manager import manager


def _families(client) -> dict:
    response = client.get("/metrics")
    assert response.status_code == 200
    return {family.name: family for family in text_string_to_metric_families(response.text)}


def _sample_value(family, **labels):
    for sample in family.samples:
        if all(sample.labels.get(k) == v for k, v in labels.items()):
            return sample.value
    return None


def test_metrics_endpoint_exposes_the_new_ws_and_simulator_series(client):
    families = _families(client)
    # Same parser quirk covered in tests/test_metrics.py: a Counter
    # family's parsed name has its "_total" suffix stripped (the
    # suffix is only re-appended at exposition/sample-name time), so
    # only the gauges below keep their literal name here.
    for name in (
        "ws_connections",
        "ws_disconnects",
        "ws_active_connections",
        "ws_event_send_failures",
        "simulator_leader_heartbeats",
        "simulator_leadership_acquired",
        "simulator_leadership_lost",
        "simulator_worker_crashes",
        "simulator_leader_last_heartbeat_timestamp_seconds",
        "simulator_leader_state",
    ):
        assert name in families, f"{name} missing from /metrics output"


def test_ws_active_connections_reflects_the_real_manager_state(client):
    # No labels on this one - a single gauge value for this process.
    before = _families(client)["ws_active_connections"].samples[0].value
    assert before == len(manager.active_connections)


def test_ws_connections_total_matches_the_manager_snapshot(client):
    families = _families(client)
    # Counter family names are parsed without their "_total" suffix
    # (see the comment on test_metrics_endpoint_exposes_the_new_ws_and_simulator_series
    # above) - "ws_connections", not "ws_connections_total".
    reported = families["ws_connections"].samples[0].value
    assert reported == manager.get_metrics_snapshot()["connects_total"]


def test_simulator_leader_state_uses_the_documented_fixed_vocabulary(client):
    families = _families(client)
    family = families["simulator_leader_state"]
    # Every loop scheduler.py knows about should have exactly one
    # sample, valued 0-3 (not_started/standby/leader/crashed).
    loops = {snap["name"] for snap in scheduler.scheduler_metrics_snapshot()}
    seen_loops = {sample.labels.get("loop") for sample in family.samples}
    assert loops <= seen_loops
    for sample in family.samples:
        assert sample.value in (0, 1, 2, 3)


def test_scraping_metrics_twice_does_not_duplicate_or_crash(client):
    # The collector is registered once at import time (module-level
    # REGISTRY.register call) - scraping repeatedly must keep working,
    # not register a second, conflicting collector each time.
    first = client.get("/metrics")
    second = client.get("/metrics")
    assert first.status_code == 200
    assert second.status_code == 200


def test_realtime_series_labels_are_short_fixed_vocabulary_strings():
    """Same cross-cutting cardinality/safety check test_metrics.py
    already applies to db_failures_total/redis_failures_total, extended
    to the new series: every label value must look like a short fixed
    identifier (an event name, a disconnect reason, a loop name),
    never anything resembling a connection string, token, or path."""
    body, _content_type = metrics.render_latest()
    rendered = body.decode("utf-8")
    for family in text_string_to_metric_families(rendered):
        if not family.name.startswith(("ws_", "simulator_")):
            continue
        for sample in family.samples:
            for value in sample.labels.values():
                assert "://" not in value
                assert "@" not in value
                assert " " not in value
