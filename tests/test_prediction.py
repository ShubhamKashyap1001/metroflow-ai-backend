from tests.conftest import NON_EXISTENT_ID


def test_crowd_prediction_requires_auth(client):
    response = client.post("/api/v1/predictions/crowd", json={"station_id": 1})
    assert response.status_code in (401, 403)


def test_traffic_pattern_requires_auth(client):
    response = client.get("/api/v1/predictions/traffic-pattern/1")
    assert response.status_code in (401, 403)


def test_crowd_prediction_rejects_missing_body(client):
    """Even unauthenticated, a completely empty POST body must not 500."""
    response = client.post("/api/v1/predictions/crowd", json={})
    assert response.status_code in (401, 403, 422)


def test_crowd_model_metrics_requires_auth(client):
    """Powers the 'AI Prediction Accuracy' KPI on the dashboard."""
    response = client.get("/api/v1/predictions/crowd/metrics")
    assert response.status_code in (401, 403)


def test_demand_forecast_requires_auth(client):
    response = client.post(
        "/api/v1/predictions/demand", json={"station_id": 1, "hours_ahead": 6}
    )
    assert response.status_code in (401, 403)


def test_delay_prediction_requires_auth(client):
    response = client.post(
        "/api/v1/predictions/delay", json={"train_id": 1, "station_id": 1}
    )
    assert response.status_code in (401, 403)


def test_frequency_recommendation_requires_auth(client):
    response = client.post(
        "/api/v1/predictions/frequency",
        json={"station_id": 1, "is_peak_hour": True},
    )
    assert response.status_code in (401, 403)


def test_delay_model_metrics_requires_auth(client):
    """Powers the delay section of the AI Prediction dashboard page."""
    response = client.get("/api/v1/predictions/delay/metrics")
    assert response.status_code in (401, 403)


def test_frequency_model_metrics_requires_auth(client):
    """Powers the frequency section of the AI Prediction dashboard page."""
    response = client.get("/api/v1/predictions/frequency/metrics")
    assert response.status_code in (401, 403)


def test_traffic_pattern_aggregate_requires_auth(client):
    response = client.get("/api/v1/predictions/traffic-pattern-aggregate/all")
    assert response.status_code in (401, 403)


def test_smart_recommendations_requires_auth(client):
    response = client.get(f"/api/v1/predictions/recommendations/{NON_EXISTENT_ID}")
    assert response.status_code in (401, 403)


# --- Phase 1 (P0-1) additions: bulk recommendations endpoint ---------------

def test_recommendations_bulk_requires_auth(client):
    """Same auth requirement as the single-station endpoint."""
    response = client.get("/api/v1/predictions/recommendations/bulk?station_ids=1,2,3")
    assert response.status_code in (401, 403)


def test_recommendations_bulk_route_not_shadowed_by_station_id_route(client):
    """`/recommendations/bulk` must be matched by the bulk route, not by
    `/recommendations/{station_id}` trying (and failing) to parse
    "bulk" as an int. If the routes were registered in the wrong order,
    this would 422 on path-param validation instead of the expected
    401/403 from the auth dependency - this test pins the registration
    order added in Phase 1."""
    response = client.get("/api/v1/predictions/recommendations/bulk?station_ids=1")
    assert response.status_code in (401, 403)