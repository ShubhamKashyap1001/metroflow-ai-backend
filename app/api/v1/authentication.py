from fastapi import APIRouter, HTTPException
from database import supabase
from passlib.context import CryptContext
from jose import jwt
import os
from datetime import datetime, timedelta


router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)


# Password encryption
pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto"
)


# JWT Configuration
SECRET_KEY = os.getenv("SECRET_KEY", "metroflow-secret-key")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60



# Create JWT token
def create_access_token(data: dict):

    to_encode = data.copy()

    expire = datetime.utcnow() + timedelta(
        minutes=ACCESS_TOKEN_EXPIRE_MINUTES
    )

    to_encode.update(
        {
            "exp": expire
        }
    )

    token = jwt.encode(
        to_encode,
        SECRET_KEY,
        algorithm=ALGORITHM
    )

    return token



# Register User
@router.post("/register")
def register(user: dict):

    email = user.get("email")
    password = user.get("password")


    if not email or not password:
        raise HTTPException(
            status_code=400,
            detail="Email and password required"
        )


    # Check existing user
    existing_user = supabase.table("users").select("*").eq(
        "email",
        email
    ).execute()


    if existing_user.data:
        raise HTTPException(
            status_code=400,
            detail="User already exists"
        )


    # Hash password
    hashed_password = pwd_context.hash(password)


    new_user = {
        "email": email,
        "password": hashed_password,
        "name": user.get("name"),
        "role": user.get("role", "user")
    }


    response = supabase.table("users").insert(
        new_user
    ).execute()


    return {
        "message": "User registered successfully",
        "user": response.data
    }




# Login User
@router.post("/login")
def login(user: dict):

    email = user.get("email")
    password = user.get("password")


    response = supabase.table("users").select("*").eq(
        "email",
        email
    ).execute()


    if not response.data:
        raise HTTPException(
            status_code=404,
            detail="User not found"
        )


    db_user = response.data[0]


    # Verify password
    if not pwd_context.verify(
        password,
        db_user["password"]
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid password"
        )


    token = create_access_token(
        {
            "email": email,
            "role": db_user.get("role")
        }
    )


    return {
        "message": "Login successful",
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "email": db_user["email"],
            "role": db_user.get("role")
        }
    }
