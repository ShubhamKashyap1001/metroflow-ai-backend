from pydantic import ConfigDict
from app.enums.alert_type import AlertType


class AlertCreate(BaseModel):
    station_id: int
    alert_type: AlertType
    message: str


class AlertResponse(BaseModel):
    id: int
    station_id: int
    alert_type: AlertType
    message: str
    model_config = ConfigDict(
        from_attributes=True
    )