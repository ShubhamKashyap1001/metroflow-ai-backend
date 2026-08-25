def test_checkin_requires_auth(client):
    payload = {"source_station_id": 1, "destination_station_id": 2}
    response = client.post("/api/v1/checkin/", json=payload)
    assert response.status_code in (401, 403)


def test_checkin_rejects_missing_source_station(client):
    response = client.post("/api/v1/checkin/", json={"destination_station_id": 2})
    assert response.status_code in (401, 403, 422)


def test_checkout_requires_auth(client):
    response = client.post("/api/v1/checkout/", json={"journey_id": 1})
    assert response.status_code in (401, 403)


def test_active_journey_requires_auth(client):
    response = client.get("/api/v1/checkout/active")
    assert response.status_code in (401, 403)