from tests.conftest import NON_EXISTENT_ID


def test_list_enquiries_requires_auth(client):
    response = client.get("/api/v1/enquiries/")
    assert response.status_code in (401, 403)


def test_create_enquiry_requires_auth(client):
    payload = {
        "category": "general",
        "subject": "Test enquiry",
        "message": "This is a test enquiry.",
    }
    response = client.post("/api/v1/enquiries/", json=payload)
    assert response.status_code in (401, 403)


def test_get_enquiry_requires_auth(client):
    """404-not-403 for someone else's enquiry is a service-layer concern,
    but with NO auth at all it must be 401/403 first."""
    response = client.get(f"/api/v1/enquiries/{NON_EXISTENT_ID}")
    assert response.status_code in (401, 403)


def test_resolve_enquiry_requires_operator_role(client):
    response = client.patch(
        f"/api/v1/enquiries/{NON_EXISTENT_ID}/resolve",
        json={"admin_reply": "Resolved."},
    )
    assert response.status_code in (401, 403)