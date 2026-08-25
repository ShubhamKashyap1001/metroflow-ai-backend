def test_health_endpoint(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200


def test_home_endpoint(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["message"] == "MetroFlow Backend Running"


def test_liveness_probe(client):
    """/healthz is the plain liveness probe - must never touch the DB,
    so it should stay 200 even if the readiness check above ever fails."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_docs_available(client):
    response = client.get("/docs")
    assert response.status_code == 200


def test_openapi_schema_available(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "paths" in schema
    # Sanity check a handful of routers actually registered.
    assert "/api/v1/health/" in schema["paths"] or "/api/v1/health" in schema["paths"]