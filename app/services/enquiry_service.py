from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload

from sqlalchemy import func

from app.enums.enquiry_status import EnquiryStatus
from app.enums.notification_source import NotificationSource
from app.enums.user_role import UserRole
from app.models.enquiry import Enquiry
from app.models.user_profile import UserProfile
from app.schemas.enquiry import EnquiryCreate, EnquiryResolve
from app.services import notification_service

def list_enquiries(
    db: Session,
    current_user: UserProfile,
    status: EnquiryStatus | None = None,
) -> list[Enquiry]:
    """Admins/operators see every enquiry (the "manage enquiries"
    queue). Passengers only ever see their own - this is what powers
    the passenger's "My Enquiries" list, and also guards
    get_enquiry() below against a passenger guessing another user's
    enquiry id."""
    query = db.query(Enquiry).options(joinedload(Enquiry.user))

    if current_user.role not in (UserRole.ADMIN, UserRole.OPERATOR):
        query = query.filter(Enquiry.user_id == current_user.id)

    if status:
        query = query.filter(Enquiry.status == status)

    return query.order_by(Enquiry.created_at.desc()).all()

def create_enquiry(db: Session, payload: EnquiryCreate, user_id) -> Enquiry:
    enquiry = Enquiry(**payload.model_dump(), user_id=user_id)
    db.add(enquiry)
    db.commit()
    db.refresh(enquiry)
    return enquiry

def get_enquiry(db: Session, enquiry_id: int, current_user: UserProfile) -> Enquiry:
    enquiry = (
        db.query(Enquiry)
        .options(joinedload(Enquiry.user))
        .filter(Enquiry.id == enquiry_id)
        .first()
    )
    if not enquiry:
        raise HTTPException(status_code=404, detail="Enquiry not found")

    is_staff = current_user.role in (UserRole.ADMIN, UserRole.OPERATOR)
    if not is_staff and str(enquiry.user_id) != str(current_user.id):
        raise HTTPException(status_code=404, detail="Enquiry not found")

    return enquiry

def resolve_enquiry(
    db: Session,
    enquiry_id: int,
    payload: EnquiryResolve,
    resolved_by,
) -> Enquiry:
    enquiry = db.get(Enquiry, enquiry_id)
    if not enquiry:
        raise HTTPException(status_code=404, detail="Enquiry not found")

    enquiry.admin_reply = payload.admin_reply
    enquiry.status = payload.status
    enquiry.resolved_by = resolved_by
    enquiry.resolved_at = (
        datetime.now(timezone.utc) if payload.status == EnquiryStatus.RESOLVED else None
    )

    db.add(enquiry)
    db.commit()
    db.refresh(enquiry)

    status_label = "resolved" if payload.status == EnquiryStatus.RESOLVED else "updated"
    notification_service.create_notification(
        db,
        source=NotificationSource.SYSTEM,
        title=f"Your enquiry \"{enquiry.subject}\" was {status_label}",
        message=payload.admin_reply,
        user_id=enquiry.user_id,
    )

    return enquiry


def get_enquiry_stats(db: Session, current_user: UserProfile) -> dict:
    query = db.query(Enquiry.status, func.count(Enquiry.id))

    if current_user.role not in (UserRole.ADMIN, UserRole.OPERATOR):
        query = query.filter(Enquiry.user_id == current_user.id)

    counts = dict(query.group_by(Enquiry.status).all())

    return {
        "total": sum(counts.values()),
        "open": counts.get(EnquiryStatus.OPEN, 0),
        "in_progress": counts.get(EnquiryStatus.IN_PROGRESS, 0),
        "resolved": counts.get(EnquiryStatus.RESOLVED, 0),
    }
