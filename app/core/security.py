"""Auth is fully delegated to Supabase now.

The frontend calls `supabase.auth.signInWithPassword()` /
`supabase.auth.signUp()` directly and gets back a Supabase-issued
JWT. This backend never sees a password - it only ever verifies the
JWT Supabase already issued, then looks up (or lazily creates) the
matching row in `user_profiles` for role-based access control.

Supports both Supabase JWT signing modes:
  - Legacy HS256 shared secret (set SUPABASE_JWT_SECRET in .env)
  - Newer asymmetric signing keys, verified via the project's JWKS
    endpoint (used automatically if SUPABASE_JWT_SECRET is empty)
"""
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
    """TTL-cached (JWKS_CACHE_TTL_SECONDS), not cached forever. A plain
    @lru_cache(maxsize=1) here would never expire, so a Supabase
    signing-key rotation would silently never be picked up without
    restarting the whole process - every subsequent token signed with
    the new key would fail to verify against the old cached JWKS
    forever. `force=True` (used below when a token's `kid` isn't in the
    cached set) lets a rotation be picked up immediately instead of
    waiting out the TTL, without giving up caching for the common case."""
    global _jwks_cache, _jwks_cache_at
    now = time.monotonic()
    if not force and _jwks_cache is not None and (now - _jwks_cache_at) < settings.JWKS_CACHE_TTL_SECONDS:
        return _jwks_cache

    with urllib.request.urlopen(settings.supabase_jwks_url, timeout=5) as resp:
        _jwks_cache = json.loads(resp.read())
    _jwks_cache_at = now
    return _jwks_cache

def _select_jwk(token: str) -> dict | list:
    """Pick the one JWK matching this token's `kid` header instead of
    handing jose the whole JWKS (every key). jose has to construct a
    cryptography key object to attempt verification with, and does
    that per key it tries - handing it just the one matching key means
    exactly one key construction per request instead of one per key in
    the set. Falls back to the full JWKS if the token has no `kid` or
    it isn't found (after one forced re-fetch, in case of an
    in-flight key rotation) - same behavior as before, just not the
    fast path."""
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
    """Rebuild a (detached, not session-bound) UserProfile from a
    cached dict, skipping the DB lookup entirely on a cache hit. `id`
    and `role` are reconstructed as the real uuid.UUID/UserRole types
    (not left as plain strings) since a couple of call sites compare
    current_user.id/.role directly (see app/api/v1/users.py) and rely
    on that."""
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
    """Call after any write to a UserProfile row (role/is_active/name/
    etc. change) so the next request doesn't keep serving the cached
    pre-update version for up to AUTH_USER_CACHE_TTL_SECONDS."""
    cache.delete(_profile_cache_key(user_id))

def _get_or_create_profile_by_email(db: Session, email: str) -> UserProfile:
    """TEMPORARY dev bypass (settings.AUTH_DISABLED=True): no Supabase
    token to read a `sub` UUID from, so derive a stable UUID from the
    email itself - the same email always maps to the same profile row."""
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
    """First request after a Supabase sign-up: create the matching
    user_profiles row. Every request after that just reads it - or,
    within AUTH_USER_CACHE_TTL_SECONDS of the last request, reads it
    from Redis instead of touching the DB at all. This runs on EVERY
    authenticated request (it's what get_current_user calls), so
    avoiding a repeated user_profiles lookup here matters a lot more
    than most other caching in the app."""
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

    if settings.AUTH_DISABLED:
                                                                      
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
    """Dependency factory for role-based access control.

    Usage: Depends(require_roles(UserRole.ADMIN, UserRole.OPERATOR))
    """

    def _dependency(current_user: UserProfile = Depends(get_current_user)) -> UserProfile:
        if current_user.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action",
            )
        return current_user

    return _dependency
