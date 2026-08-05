from pydantic import BaseModel
from pydantic import ConfigDict
from app.enums.crowd_level import CrowdLevel


class CrowdLogCreate(BaseModel):
    station_id: int
    current_count: int
    crowd_level: CrowdLevel


class CrowdLogResponse(BaseModel):
    id: int
    station_id: int
    current_count: int
    crowd_level: CrowdLevel
    model_config = ConfigDict(
        from_attributes=True
    )