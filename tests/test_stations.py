from tests.conftest import NON_EXISTENT_ID

VALID_STATION_PAYLOAD = {
    "station_code": "TST01",
    "station_name": "Test Station",
    "city": "TestCity",
    "latitude": 28.6139,
    "longitude": 77.2090,
    "is_interchange": False,
    "capacity": 5000,
}


def test_list_stations(client):
    response = client.get("/api/v1/stations/")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_list_stations_filter_by_city(client):
    response = client.get("/api/v1/stations/", params={"city": "Delhi"})
    assert response.status_code == 200


def test_list_stations_filter_by_state(client):
    response = client.get("/api/v1/stations/", params={"state": "Delhi"})
    assert response.status_code == 200


def test_get_nonexistent_station_returns_404(client):
    response = client.get(f"/api/v1/stations/{NON_EXISTENT_ID}")
    assert response.status_code == 404


def test_create_station_requires_auth(client):
    response = client.post("/api/v1/stations/", json=VALID_STATION_PAYLOAD)
    assert response.status_code in (401, 403)


def test_create_station_rejects_incomplete_payload(client):
    """Even unauthenticated, a garbage payload should not somehow reach
    the DB layer - either 401/403 (auth checked first) or 422 (validation
    checked first) is acceptable, but never a 500."""
    response = client.post("/api/v1/stations/", json={"station_name": "Missing Fields"})
    assert response.status_code in (401, 403, 422)


def test_update_station_requires_auth(client):
    response = client.put(f"/api/v1/stations/{NON_EXISTENT_ID}", json={"station_name": "X"})
    assert response.status_code in (401, 403)


def test_delete_station_requires_admin(client):
    """Delete is ADMIN-only (stricter than create/update which allow OPERATOR too)."""
    response = client.delete(f"/api/v1/stations/{NON_EXISTENT_ID}")
    assert response.status_code in (401, 403)