from pydantic import BaseModel
from pydantic import ConfigDict
from app.enums.line_status import LineStatus


class MetroLineCreate(BaseModel):
    line_code: str
    line_name: str
    color: str


class MetroLineUpdate(BaseModel):
    line_name: str | None = None
    color: str | None = None
    status: LineStatus | None = None
    is_active: bool | None = None


class MetroLineResponse(BaseModel):
    id: int
    line_code: str
    line_name: str
    color: str
    status: LineStatus
    is_active: bool
    model_config = ConfigDict(
        from_attributes=True
    )