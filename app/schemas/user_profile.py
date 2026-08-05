from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from app.enums.user_role import UserRole


class UserProfileCreate(BaseModel):

    full_name: str
    phone: str | None = None
    avatar_url: str | None = None


class UserProfileUpdate(BaseModel):

    full_name: str | None = None
    phone: str | None = None
    avatar_url: str | None = None
    role: UserRole | None = None
    is_active: bool | None = None


class UserProfileResponse(BaseModel):

    id: UUID
    full_name: str
    phone: str | None
    avatar_url: str | None
    role: UserRole
    is_active: bool
    model_config = ConfigDict(
        from_attributes=True
    )