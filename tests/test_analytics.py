def test_traffic_report_requires_auth(client):
    response = client.get("/api/v1/analytics/traffic-report")
    assert response.status_code in (401, 403)

def test_operational_summary_requires_auth(client):
    response = client.get("/api/v1/analytics/operational-summary")
    assert response.status_code in (401, 403)

def test_passenger_flow_overview_requires_auth(client):
    response = client.get("/api/v1/analytics/passenger-flow-overview")
    assert response.status_code in (401, 403)

def test_prediction_insights_requires_auth(client):
    response = client.get("/api/v1/analytics/prediction-insights")
    assert response.status_code in (401, 403)

def test_data_freshness_requires_operational_auth(client):
    response = client.get("/api/v1/analytics/data-freshness")
    assert response.status_code in (401, 403)

def test_traffic_comparison_requires_operational_auth(client):
    response = client.get(
        "/api/v1/analytics/traffic-comparison",
        params={
            "start_date": "2026-10-20",
            "end_date": "2026-10-26",
            "previous_start_date": "2026-10-13",
            "previous_end_date": "2026-10-19",
        },
    )
    assert response.status_code in (401, 403)

def test_report_schedules_require_operational_auth(client):
    response = client.get("/api/v1/analytics/report-schedules")
    assert response.status_code in (401, 403)

def test_traffic_report_bad_hours_param_never_500s(client):
    response = client.get("/api/v1/analytics/traffic-report", params={"hours": "not-a-number"})
    assert response.status_code in (401, 403, 422)
