import json
import time
import urllib.request
import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.core import cache
from app.core.config import settings
from app.database.session import get_db
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/me", auto_error=False)

_jwks_cache: dict | None = None
_jwks_cache_at: float = 0.0

_JWKS_TTL = settings.JWKS_CACHE_TTL_SECONDS

def _fetch_jwks(force: bool = False) -> dict:
    global _jwks_cache, _jwks_cache_at
    now = time.monotonic()
    if not force and _jwks_cache is not None and (now - _jwks_cache_at) < settings.JWKS_CACHE_TTL_SECONDS:
        return _jwks_cache

    with urllib.request.urlopen(settings.supabase_jwks_url, timeout=5) as resp:
        _jwks_cache = json.loads(resp.read())
    _jwks_cache_at = now
    return _jwks_cache

def _select_jwk(token: str) -> dict | list:
    jwks = _fetch_jwks()
    unverified_kid = jwt.get_unverified_header(token).get("kid")
    if not unverified_kid:
        return jwks

    for key in jwks.get("keys", []):
        if key.get("kid") == unverified_kid:
            return key

    jwks = _fetch_jwks(force=True)
    for key in jwks.get("keys", []):
        if key.get("kid") == unverified_kid:
            return key
    return jwks

def _decode_supabase_token(token: str) -> dict:
    try:
        if settings.SUPABASE_JWT_SECRET:
                                   
            return jwt.decode(
                token,
                settings.SUPABASE_JWT_SECRET,
                algorithms=["HS256"],
                audience="authenticated",
            )

        key = _select_jwk(token)
        return jwt.decode(
            token,
            key,
            algorithms=["RS256", "ES256"],
            audience="authenticated",
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired Supabase session token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except HTTPException:
                                                                       
        raise
    except Exception:
                                                                   
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not verify session (auth service unreachable). Please try again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

_PROFILE_FIELDS = ("id", "email", "full_name", "username", "phone", "avatar_url", "role", "is_active")

def _profile_cache_key(user_id: uuid.UUID) -> str:
    return f"user:profile:{user_id}"

def _serialize_profile(u: UserProfile) -> dict:
    return {field: getattr(u, field) for field in _PROFILE_FIELDS}

def _hydrate_profile(d: dict) -> UserProfile:
    return UserProfile(
        id=uuid.UUID(d["id"]) if isinstance(d["id"], str) else d["id"],
        email=d["email"],
        full_name=d["full_name"],
        username=d["username"],
        phone=d["phone"],
        avatar_url=d["avatar_url"],
        role=UserRole(d["role"]),
        is_active=d["is_active"],
    )

def invalidate_user_cache(user_id: uuid.UUID) -> None:
    cache.delete(_profile_cache_key(user_id))

def _get_or_create_profile_by_email(db: Session, email: str) -> UserProfile:
    email = email.strip().lower()
    user_id = uuid.uuid5(uuid.NAMESPACE_DNS, email)

    cached = cache.get_json(_profile_cache_key(user_id))
    if cached is not None:
        return _hydrate_profile(cached)

    user = db.get(UserProfile, user_id)
    if not user:
        user = UserProfile(
            id=user_id,
            email=email,
            full_name=email.split("@")[0],
            role=UserRole.PASSENGER,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    cache.set_json(_profile_cache_key(user_id), _serialize_profile(user), ttl_seconds=settings.AUTH_USER_CACHE_TTL_SECONDS)
    return user

def _get_or_create_profile(db: Session, payload: dict) -> UserProfile:
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token missing subject claim")

    try:
        user_id = uuid.UUID(str(user_id))
    except ValueError:
        raise HTTPException(status_code=401, detail="Token subject is not a valid UUID")

    cached = cache.get_json(_profile_cache_key(user_id))
    if cached is not None:
        return _hydrate_profile(cached)

    user = db.get(UserProfile, user_id)
    if not user:
        metadata = payload.get("user_metadata") or {}

        raw_email = payload.get("email")
        email = raw_email.strip().lower() if raw_email else None

        phone = payload.get("phone") or metadata.get("phone")

        full_name = (
            metadata.get("full_name")
            or metadata.get("name")
            or (email or "").split("@")[0]
            or phone
            or "MetroFlow User"
        )

        user = UserProfile(
            id=user_id,
            email=email,
            full_name=full_name,
            username=metadata.get("username") or None,
            phone=phone,
            avatar_url=metadata.get("avatar_url") or metadata.get("picture"),
            role=UserRole.PASSENGER,                                                     
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    cache.set_json(_profile_cache_key(user_id), _serialize_profile(user), ttl_seconds=settings.AUTH_USER_CACHE_TTL_SECONDS)
    return user

def get_user_from_token_optional(token: str | None, db: Session) -> UserProfile | None:
    if not token:
        return None
    try:
        if settings.dev_auth_bypass_enabled:
            user = _get_or_create_profile_by_email(db, token)
        else:
            payload = _decode_supabase_token(token)
            user = _get_or_create_profile(db, payload)
        if not user.is_active:
            return None
        return user
    except HTTPException:
        return None
    except Exception:
        return None

def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> UserProfile:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if settings.dev_auth_bypass_enabled:

        user = _get_or_create_profile_by_email(db, token)
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account is disabled")
        return user

    payload = _decode_supabase_token(token)
    user = _get_or_create_profile(db, payload)

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    return user

def require_roles(*roles: UserRole):

    def _dependency(current_user: UserProfile = Depends(get_current_user)) -> UserProfile:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action",
            )
        return current_user

    return _dependency
