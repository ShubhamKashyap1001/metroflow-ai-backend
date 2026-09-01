from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from app.enums.crowd_level import CrowdLevel

class CrowdLogCreate(BaseModel):
    station_id: int
    # Phase 3 fix (docs/crowd-data-correctness.md, Bug 4): this had no lower
    # bound, so a manual/sensor POST /crowd/ with a negative count (a
    # typo, a buggy upstream sensor feed, etc.) sailed straight through
    # into crowd_logs AND station_crowd_state - negative occupancy is
    # never physically valid, so reject it at the API boundary instead
    # of downstream.
    current_count: int = Field(ge=0)
    crowd_level: CrowdLevel | None = None

class CrowdLogResponse(BaseModel):
    id: int
    station_id: int
    current_count: int
    crowd_level: CrowdLevel
    model_config = ConfigDict(
        from_attributes=True
    )