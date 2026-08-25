from tests.conftest import NON_EXISTENT_ID


def test_list_alerts_requires_auth(client):
    response = client.get("/api/v1/alerts/")
    assert response.status_code in (401, 403)


def test_get_alert_requires_auth(client):
    response = client.get(f"/api/v1/alerts/{NON_EXISTENT_ID}")
    assert response.status_code in (401, 403)


def test_create_alert_requires_operator_role(client):
    """Overcrowding / delay / emergency alerts - ADMIN/OPERATOR only."""
    payload = {
        "station_id": 1,
        "alert_type": "overcrowding",
        "message": "Test alert",
    }
    response = client.post("/api/v1/alerts/", json=payload)
    assert response.status_code in (401, 403)


def test_resolve_alert_requires_operator_role(client):
    response = client.patch(f"/api/v1/alerts/{NON_EXISTENT_ID}/resolve", json={})
    assert response.status_code in (401, 403)


def test_alert_notifications_log_requires_operator_role(client):
    """Per-recipient email/SMS delivery status - ADMIN/OPERATOR only."""
    response = client.get(f"/api/v1/alerts/{NON_EXISTENT_ID}/notifications")
    assert response.status_code in (401, 403)


def test_list_notifications_requires_auth(client):
    response = client.get("/api/v1/notifications/")
    assert response.status_code in (401, 403)


def test_unread_count_requires_auth(client):
    response = client.get("/api/v1/notifications/unread-count")
    assert response.status_code in (401, 403)


def test_mark_notification_read_requires_auth(client):
    response = client.patch(f"/api/v1/notifications/{NON_EXISTENT_ID}/read")
    assert response.status_code in (401, 403)


def test_mark_all_notifications_read_requires_auth(client):
    response = client.patch("/api/v1/notifications/read-all")
    assert response.status_code in (401, 403)