from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

class NewsCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    content: str = Field(min_length=3, max_length=2000)

class NewsUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    content: str | None = Field(default=None, min_length=3, max_length=2000)
    is_active: bool | None = None

class NewsResponse(BaseModel):
    id: int
    title: str
    content: str
    created_by: UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    model_config = ConfigDict(from_attributes=True)
