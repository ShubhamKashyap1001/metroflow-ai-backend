def test_simulator_status_requires_operator_role(client):
    response = client.get("/api/v1/admin/simulator")
    assert response.status_code in (401, 403)


def test_simulator_start_requires_operator_role(client):
    response = client.post("/api/v1/admin/simulator/start")
    assert response.status_code in (401, 403)


def test_simulator_stop_requires_operator_role(client):
    response = client.post("/api/v1/admin/simulator/stop")
    assert response.status_code in (401, 403)


def test_train_tracker_start_requires_operator_role(client):
    response = client.post("/api/v1/admin/train-tracker/start")
    assert response.status_code in (401, 403)


def test_train_tracker_stop_requires_operator_role(client):
    response = client.post("/api/v1/admin/train-tracker/stop")
    assert response.status_code in (401, 403)