def test_traffic_report_requires_auth(client):
    response = client.get("/api/v1/analytics/traffic-report")
    assert response.status_code in (401, 403)


def test_operational_summary_requires_auth(client):
    response = client.get("/api/v1/analytics/operational-summary")
    assert response.status_code in (401, 403)


def test_passenger_flow_overview_requires_auth(client):
    """Powers Analytics page KPI cards + 'Passenger Flow by Station' widget."""
    response = client.get("/api/v1/analytics/passenger-flow-overview")
    assert response.status_code in (401, 403)


def test_prediction_insights_requires_auth(client):
    response = client.get("/api/v1/analytics/prediction-insights")
    assert response.status_code in (401, 403)


def test_traffic_report_bad_hours_param_never_500s(client):
    """Auth is checked regardless of query param validity - still no 500."""
    response = client.get("/api/v1/analytics/traffic-report", params={"hours": "not-a-number"})
    assert response.status_code in (401, 403, 422)