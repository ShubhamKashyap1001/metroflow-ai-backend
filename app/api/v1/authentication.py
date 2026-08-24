"""Auth itself (sign up / login / password reset / sessions) is fully
handled by Supabase on the frontend - see the frontend's
`src/lib/supabase/client.ts` and `src/app/(auth)/login/page.tsx`.

This backend only ever verifies the Supabase-issued JWT sent in the
`Authorization: Bearer <token>` header and exposes the resulting
profile. There is no /register or /login endpoint here on purpose.
"""
from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.models.user_profile import UserProfile
from app.schemas.user_profile import UserProfileResponse

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)

@router.get("/me", response_model=UserProfileResponse)
def read_current_user(current_user: UserProfile = Depends(get_current_user)):
    """Verifies the Supabase session token and returns (creating on
    first call) the matching app profile - role, name, etc."""
    return current_user
