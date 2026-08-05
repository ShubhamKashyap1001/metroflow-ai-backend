from pydantic import BaseModel
from pydantic import ConfigDict


class PredictionCreate(BaseModel):
    station_id: int
    predicted_count: int
    confidence: float


class PredictionResponse(BaseModel):
    id: int
    station_id: int
    predicted_count: int
    confidence: float
    model_config = ConfigDict(
        from_attributes=True
    )