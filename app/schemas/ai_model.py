from pydantic import BaseModel
from pydantic import ConfigDict


class AIModelCreate(BaseModel):
    model_name: str
    version: str
    accuracy: float


class AIModelResponse(BaseModel):
    id: int
    model_name: str
    version: str
    accuracy: float
    model_config = ConfigDict(
        from_attributes=True
    )