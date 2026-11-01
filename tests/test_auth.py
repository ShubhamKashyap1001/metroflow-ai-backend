from tests.conftest import INVALID_BEARER

def test_me_requires_auth(client):
    response = client.get("/api/v1/auth/me")
    assert response.status_code in (401, 403)

def test_me_rejects_malformed_bearer_token(client):
    response = client.get("/api/v1/auth/me", headers=INVALID_BEARER)
    assert response.status_code in (401, 403)

def test_me_rejects_empty_bearer_token(client):
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer "})
    assert response.status_code in (401, 403)

def test_me_rejects_wrong_auth_scheme(client):
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code in (401, 403)
