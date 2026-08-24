def test_crowd_prediction_requires_auth(client):
    response = client.post("/api/v1/predictions/crowd", json={"station_id": 1})
    assert response.status_code in (401, 403)

def test_traffic_pattern_requires_auth(client):
    response = client.get("/api/v1/predictions/traffic-pattern/1")
    assert response.status_code in (401, 403)
