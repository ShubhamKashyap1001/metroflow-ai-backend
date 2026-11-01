from tests.conftest import NON_EXISTENT_ID

def test_crowd_dashboard(client):
    response = client.get("/api/v1/crowd/dashboard")
    assert response.status_code == 200

def test_crowd_dashboard_filter_by_state(client):
    response = client.get("/api/v1/crowd/dashboard", params={"state": "Maharashtra"})
    assert response.status_code == 200

def test_crowd_heatmap(client):
    response = client.get("/api/v1/crowd/heatmap")
    assert response.status_code == 200

def test_crowd_heatmap_top_n_limit(client):
    response = client.get("/api/v1/crowd/heatmap", params={"limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert len(body) <= 5

def test_crowd_congestion_default_threshold(client):
    response = client.get("/api/v1/crowd/congestion")
    assert response.status_code == 200

def test_crowd_congestion_invalid_level_rejected(client):
    response = client.get("/api/v1/crowd/congestion", params={"min_level": "not-a-level"})
    assert response.status_code == 422

def test_get_station_crowd_never_500s_for_unknown_station(client):
    response = client.get(f"/api/v1/crowd/{NON_EXISTENT_ID}")
    assert response.status_code == 200

def test_inflow_outflow(client):
    response = client.get(f"/api/v1/crowd/{NON_EXISTENT_ID}/inflow-outflow")
    assert response.status_code in (200, 404)

def test_station_analytics(client):
    response = client.get(f"/api/v1/crowd/{NON_EXISTENT_ID}/analytics")
    assert response.status_code in (200, 404)

def test_log_crowd_valid_payload(client):
    payload = {"station_id": NON_EXISTENT_ID, "current_count": 120}
    response = client.post("/api/v1/crowd/", json=payload)
    assert response.status_code in (201, 400, 404, 422)

def test_log_crowd_rejects_missing_required_field(client):
    response = client.post("/api/v1/crowd/", json={"station_id": 1})
    assert response.status_code == 422

def test_log_crowd_rejects_wrong_type(client):
    response = client.post(
        "/api/v1/crowd/", json={"station_id": "not-an-int", "current_count": 10}
    )
    assert response.status_code == 422
