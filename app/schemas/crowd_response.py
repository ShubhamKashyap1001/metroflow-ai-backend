from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

class CrowdResponseCreate(BaseModel):
    station_id: int
    action_type: Literal["staff_dispatch", "passenger_advisory"]

class CrowdResponseUpdate(BaseModel):
    status: Literal["in_progress", "completed", "cancelled"] | None = None
    assign_to_me: bool = False

class CrowdResponseActionResponse(BaseModel):
    id: int
    station_id: int
    station_name: str | None = None
    action_type: str
    status: str
    message: str
    created_by_id: UUID
    assigned_to_id: UUID | None
    assignee_name: str | None = None
    linked_alert_id: int | None
    notifications_delivered: bool | None = None
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)
