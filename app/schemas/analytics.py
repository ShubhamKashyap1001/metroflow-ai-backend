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

    window_hours: float
    total_inflow: int
    total_outflow: int
    net_flow: int
    avg_predicted_occupancy: float
    top_stations: list[PassengerFlowStationRow]
    ridership_by_line: list[RidershipByLineRow]
    generated_at: datetime

class PassengerTravelPrediction(BaseModel):
    id: int
    station_id: int
    prediction_type: str
    predicted_value: float
    target_datetime: datetime | None
    created_at: datetime

class PublicTravelAdvisory(BaseModel):
    id: int
    station_id: int
    alert_type: str
    message: str
    available_until: datetime | None
    created_at: datetime

class PassengerTravelUpdates(BaseModel):
    predictions: list[PassengerTravelPrediction]
    advisories: list[PublicTravelAdvisory]
