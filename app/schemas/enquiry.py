from datetime import datetime
from uuid import UUID

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from app.enums.enquiry_category import EnquiryCategory
from app.enums.enquiry_status import EnquiryStatus

class EnquiryCreate(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    category: EnquiryCategory = EnquiryCategory.GENERAL
    message: str = Field(min_length=5, max_length=1000)

class EnquiryResolve(BaseModel):
    """Admin/operator response. `status` defaults to RESOLVED, but can
    be set to IN_PROGRESS to log an interim reply without closing the
    enquiry yet (e.g. "we're looking into it")."""
    admin_reply: str = Field(min_length=1, max_length=1000)
    status: EnquiryStatus = EnquiryStatus.RESOLVED

class EnquirerSummary(BaseModel):
    id: UUID
    full_name: str
    email: str | None = None
    model_config = ConfigDict(from_attributes=True)

class EnquiryResponse(BaseModel):
    id: int
    user_id: UUID
    subject: str
    category: EnquiryCategory
    message: str
    status: EnquiryStatus
    admin_reply: str | None
    resolved_by: UUID | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    user: EnquirerSummary | None = None
    model_config = ConfigDict(from_attributes=True)
