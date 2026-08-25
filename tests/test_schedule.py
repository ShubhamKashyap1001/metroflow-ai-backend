from tests.conftest import NON_EXISTENT_ID


def test_list_schedules(client):
    response = client.get("/api/v1/schedules/")
    assert response.status_code == 200


def test_list_schedules_filter_by_state(client):
    response = client.get("/api/v1/schedules/", params={"state": "Karnataka"})
    assert response.status_code == 200


def test_peak_hour_schedules(client):
    response = client.get("/api/v1/schedules/peak-hours")
    assert response.status_code == 200


def test_delayed_schedules(client):
    response = client.get("/api/v1/schedules/delayed")
    assert response.status_code == 200


def test_get_nonexistent_schedule_returns_404(client):
    response = client.get(f"/api/v1/schedules/{NON_EXISTENT_ID}")
    assert response.status_code == 404


def test_create_schedule_requires_auth(client):
    payload = {
        "train_id": 1,
        "station_id": 1,
        "arrival_time": "08:00:00",
        "departure_time": "08:02:00",
        "platform_number": 1,
    }
    response = client.post("/api/v1/schedules/", json=payload)
    assert response.status_code in (401, 403)


def test_update_schedule_requires_auth(client):
    response = client.put(f"/api/v1/schedules/{NON_EXISTENT_ID}", json={"platform_number": 2})
    assert response.status_code in (401, 403)


def test_report_delay_requires_auth(client):
    """Delay handling workflow - core Scheduling Management Module requirement."""
    response = client.patch(
        f"/api/v1/schedules/{NON_EXISTENT_ID}/delay",
        json={"delay_minutes": 5, "reason": "signal failure"},
    )
    assert response.status_code in (401, 403)


def test_adjust_frequency_requires_auth(client):
    """Frequency adjustment workflow - core Scheduling Management Module requirement."""
    response = client.patch(
        f"/api/v1/schedules/{NON_EXISTENT_ID}/frequency",
        json={"frequency_minutes": 5},
    )
    assert response.status_code in (401, 403)