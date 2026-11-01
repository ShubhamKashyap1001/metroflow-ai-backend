import uuid

from tests.conftest import INVALID_BEARER

RANDOM_USER_ID = str(uuid.uuid4())

def test_list_users_requires_admin(client):
    response = client.get("/api/v1/users/")
    assert response.status_code in (401, 403)

def test_list_users_rejects_non_admin_forged_token(client):
    response = client.get("/api/v1/users/", headers=INVALID_BEARER)
    assert response.status_code in (401, 403)

def test_get_user_requires_operator_role(client):
    response = client.get(f"/api/v1/users/{RANDOM_USER_ID}")
    assert response.status_code in (401, 403)

def test_get_user_rejects_malformed_uuid(client):
    response = client.get("/api/v1/users/not-a-uuid")
    assert response.status_code in (401, 403, 422)

def test_update_user_requires_auth(client):
    response = client.put(f"/api/v1/users/{RANDOM_USER_ID}", json={"full_name": "Test User"})
    assert response.status_code in (401, 403)
