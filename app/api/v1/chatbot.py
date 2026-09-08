from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.rate_limit import AI_LIMIT, limiter
from app.core.security import get_current_user
from app.database.session import get_db
from app.models.user_profile import UserProfile
from app.schemas.chatbot import ChatRequest, ChatResponse
from app.services import chatbot_service

router = APIRouter(
    prefix="/chatbot",
    tags=["Chatbot"]
)

@router.post("/message", response_model=ChatResponse)
@limiter.limit(AI_LIMIT)
def send_chat_message(
    request: Request,
    payload: ChatRequest,
    db: Session = Depends(get_db),
    current_user: UserProfile = Depends(get_current_user),
):
    reply = chatbot_service.send_message(db, payload.messages)
    return ChatResponse(reply=reply)
