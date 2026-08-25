from tests.conftest import INVALID_BEARER


def test_me_requires_auth(client):
    response = client.get("/api/v1/auth/me")
    assert response.status_code in (401, 403)


def test_me_rejects_malformed_bearer_token(client):
    """A garbage/forged JWT must be rejected (401), not crash the app (500)."""
    response = client.get("/api/v1/auth/me", headers=INVALID_BEARER)
    assert response.status_code in (401, 403)


def test_me_rejects_empty_bearer_token(client):
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer "})
    assert response.status_code in (401, 403)


def test_me_rejects_wrong_auth_scheme(client):
    """Basic auth (or any non-Bearer scheme) must not be accepted."""
    response = client.get("/api/v1/auth/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code in (401, 403)