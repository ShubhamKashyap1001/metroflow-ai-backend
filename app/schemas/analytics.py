from datetime import datetime

from pydantic import BaseModel

class PassengerFlowStationRow(BaseModel):
    station_id: int
    station_name: str
    entries: int
    exits: int

class RidershipByLineRow(BaseModel):
    line_name: str
    color: str
    passenger_count: int

class PassengerFlowOverview(BaseModel):
    """Powers the "Passenger Flow by Station" chart, the four KPI cards
    (Total Inflow / Total Outflow / Net Flow / Avg Predicted Occupancy)
    and the "Ridership by Line" donut on the Analytics page.

    `window_hours` is a float (not int) so callers can ask for a short,
    visibly-live rolling window (e.g. 0.5 = last 30 minutes) instead of
    only whole-hour windows - see analytics_service.passenger_flow_overview
    for why a short window matters here.
    """

    window_hours: float
    total_inflow: int
    total_outflow: int
    net_flow: int
    avg_predicted_occupancy: float
    top_stations: list[PassengerFlowStationRow]
    ridership_by_line: list[RidershipByLineRow]
    generated_at: datetime
