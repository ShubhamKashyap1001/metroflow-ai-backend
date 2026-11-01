from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.enums.alert_type import AlertType


class AlertCreate(BaseModel):
    station_id: int
    alert_type: AlertType
    message: str
    notify_email: bool = True
    notify_sms: bool = True
    available_until: datetime | None = None


class AlertResolve(BaseModel):
    notify_on_resolve: bool = True


class AlertResponse(BaseModel):
    id: int
    station_id: int
    alert_type: AlertType
    message: str
    is_resolved: bool
    resolved_at: datetime | None
    available_until: datetime | None
    notify_email: bool
    notify_sms: bool
    acknowledged_at: datetime | None = None
    acknowledged_by_id: UUID | None = None
    acknowledged_by_name: str | None = None
    assigned_to_id: UUID | None = None
    assigned_to_name: str | None = None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


class AlertAcknowledge(BaseModel):
    note: str | None = None


class AlertAssignment(BaseModel):
    assigned_to_id: UUID | None = None


class AlertIncidentEventResponse(BaseModel):
    id: int
    alert_id: int
    actor_id: UUID | None
    actor_name: str | None = None
    action: str
    note: str | None
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
