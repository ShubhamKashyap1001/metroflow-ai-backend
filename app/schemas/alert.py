from datetime import datetime

from pydantic import BaseModel
from pydantic import ConfigDict

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
    created_at: datetime
    model_config = ConfigDict(
        from_attributes=True
    )
