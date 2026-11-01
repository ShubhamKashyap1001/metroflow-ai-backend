import sys

from sqlalchemy import text

from app.database.session import SessionLocal
from app.enums.user_role import UserRole
from app.models.user_profile import UserProfile

def set_role(email: str, role_value: str) -> None:
    role = UserRole(role_value)                                
    db = SessionLocal()

    try:
        row = db.execute(
            text("SELECT id, email FROM auth.users WHERE email = :email"),
            {"email": email},
        ).first()

        if not row:
            print(f"No Supabase account found for {email}. "
                  f"Ask them to sign up first, then re-run this script.")
            return

        user_id, user_email = row
        if isinstance(user_id, str):
            import uuid as uuid_lib
            user_id = uuid_lib.UUID(user_id)

        profile = db.get(UserProfile, user_id)
        if profile is None:
                                                                         
            profile = UserProfile(
                id=user_id,
                email=user_email,
                full_name=user_email.split("@")[0],
                role=role,
            )
            db.add(profile)
        else:
            profile.role = role

        db.commit()
        print(f"{email} is now '{role.value}'.")

    finally:
        db.close()

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m app.database.set_user_role <email> <admin|operator|passenger>")
        sys.exit(1)

    set_role(sys.argv[1], sys.argv[2])
