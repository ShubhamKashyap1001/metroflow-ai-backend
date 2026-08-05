from pydantic import BaseModel
from pydantic import ConfigDict

class LineStationCreate(BaseModel):
    line_id: int
    station_id: int
    station_order: int
    distance_from_previous: float = 0


class LineStationResponse(BaseModel):
    id: int
    line_id: int
    station_id: int
    station_order: int
    distance_from_previous: float
    model_config = ConfigDict(
        from_attributes=True
    )