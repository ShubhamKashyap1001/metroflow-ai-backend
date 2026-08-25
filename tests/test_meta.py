def test_list_states(client):
    response = client.get("/api/v1/meta/states")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    if body:
        row = body[0]
        assert {"state", "cities", "station_count", "train_count", "has_sufficient_data"} <= row.keys()


def test_list_cities(client):
    response = client.get("/api/v1/meta/cities")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_nearest_city_requires_lat_lng(client):
    """lat/lng are required Query(...) params - omitting them must 422, not 500."""
    response = client.get("/api/v1/meta/nearest-city")
    assert response.status_code == 422


def test_nearest_city_rejects_out_of_range_latitude(client):
    response = client.get("/api/v1/meta/nearest-city", params={"lat": 200, "lng": 77.2})
    assert response.status_code == 422


def test_nearest_city_rejects_out_of_range_longitude(client):
    response = client.get("/api/v1/meta/nearest-city", params={"lat": 28.6, "lng": -400})
    assert response.status_code == 422


def test_nearest_city_valid_coordinates(client):
    """Real Delhi-ish coordinates - either finds a nearest seeded city (200)
    or, on a totally empty DB, correctly reports none to match against (404).
    Either is a valid, non-crashing outcome."""
    response = client.get("/api/v1/meta/nearest-city", params={"lat": 28.6139, "lng": 77.2090})
    assert response.status_code in (200, 404)
    if response.status_code == 200:
        body = response.json()
        assert {"city", "state", "distance_km"} <= body.keys()