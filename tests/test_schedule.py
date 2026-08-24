def test_list_schedules(client):
    response = client.get("/api/v1/schedules/")
    assert response.status_code == 200

def test_peak_hour_schedules(client):
    response = client.get("/api/v1/schedules/peak-hours")
    assert response.status_code == 200

def test_delayed_schedules(client):
    response = client.get("/api/v1/schedules/delayed")
    assert response.status_code == 200
