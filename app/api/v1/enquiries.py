
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.security import get_current_user, require_roles
from app.database.session import get_db
from app.enums.enquiry_status import EnquiryStatus
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile
from app.schemas.enquiry import EnquiryCreate, EnquiryResolve, EnquiryResponse
from app.services import enquiry_service

from app.schemas.enquiry import EnquiryCreate, EnquiryResolve, EnquiryResponse, EnquiryStats

router = APIRouter(
    prefix="/enquiries",
    tags=["Enquiries"]
)

@router.get("/", response_model=list[EnquiryResponse])
def get_enquiries(
    status: EnquiryStatus | None = None,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return enquiry_service.list_enquiries(db, current_user, status)

@router.post("/", response_model=EnquiryResponse, status_code=201)
def create_enquiry(
    payload: EnquiryCreate,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return enquiry_service.create_enquiry(db, payload, user_id=current_user.id)

@router.get("/stats/summary", response_model=EnquiryStats)
def get_enquiry_stats(
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return enquiry_service.get_enquiry_stats(db, current_user)

@router.get("/{enquiry_id}", response_model=EnquiryResponse)
def get_enquiry(
    enquiry_id: int,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    return enquiry_service.get_enquiry(db, enquiry_id, current_user)

@router.patch("/{enquiry_id}/resolve", response_model=EnquiryResponse)
def resolve_enquiry(
    enquiry_id: int,
    payload: EnquiryResolve,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR)),
):
    return enquiry_service.resolve_enquiry(db, enquiry_id, payload, resolved_by=current_user.id)
