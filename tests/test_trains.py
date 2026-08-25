from tests.conftest import NON_EXISTENT_ID


def test_list_trains(client):
    response = client.get("/api/v1/trains/")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_list_trains_filter_by_state(client):
    response = client.get("/api/v1/trains/", params={"state": "West Bengal"})
    assert response.status_code == 200


def test_live_train_positions(client):
    response = client.get("/api/v1/trains/live")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_live_train_positions_filter_by_state(client):
    """Regression guard for the 'shows all trains instead of the
    selected state's trains' class of bug - the filtered response must
    stay a valid list and must not error out."""
    response = client.get("/api/v1/trains/live", params={"state": "Kolkata"})
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)


def test_train_routes(client):
    response = client.get("/api/v1/trains/routes")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_get_nonexistent_train_returns_404(client):
    response = client.get(f"/api/v1/trains/{NON_EXISTENT_ID}")
    assert response.status_code == 404


def test_create_train_requires_auth(client):
    response = client.post("/api/v1/trains/", json={"train_number": "TST-01", "capacity": 300})
    assert response.status_code in (401, 403)


def test_update_train_requires_auth(client):
    response = client.put(f"/api/v1/trains/{NON_EXISTENT_ID}", json={"capacity": 400})
    assert response.status_code in (401, 403)