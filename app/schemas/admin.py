from pydantic import BaseModel, Field

class SimulatorSettingsUpdate(BaseModel):
    crowd_interval_seconds: int | None = Field(default=None, ge=5, le=3600)
    train_interval_seconds: int | None = Field(default=None, ge=5, le=3600)
