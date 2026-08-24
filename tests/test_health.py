def test_health_endpoint(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200

def test_home_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["message"] == "MetroFlow Backend Running"
