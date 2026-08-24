from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict

from app.enums.user_role import UserRole

class UserProfileUpdate(BaseModel):
    full_name: str | None = None
    username: str | None = None
    phone: str | None = None
    avatar_url: str | None = None
    role: UserRole | None = None
    is_active: bool | None = None

class UserProfileResponse(BaseModel):
    id: UUID
    email: str | None
    full_name: str
    username: str | None
    phone: str | None
    avatar_url: str | None
    role: UserRole
    is_active: bool
    model_config = ConfigDict(
        from_attributes=True
    )
