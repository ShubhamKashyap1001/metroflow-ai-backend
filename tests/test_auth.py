def test_me_requires_auth(client):
                                                       
    response = client.get("/api/v1/auth/me")
    assert response.status_code in (401, 403)
